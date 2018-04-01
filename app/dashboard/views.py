# -*- coding: utf-8 -*-
'''
    Copyright (C) 2017 Gitcoin Core

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <http://www.gnu.org/licenses/>.

'''
from __future__ import print_function, unicode_literals

import json
import logging
import time

from django.conf import settings
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from app.utils import ellipses, sync_profile
from dashboard.models import (
    Bounty, CoinRedemption, CoinRedemptionRequest, Interest, Profile, ProfileSerializer, Subscription, Tip, Tool,
    ToolVote, UserAction,
)
from dashboard.notifications import (
    maybe_market_tip_to_email, maybe_market_tip_to_github, maybe_market_tip_to_slack, maybe_market_to_slack,
    maybe_market_to_twitter,
)
from dashboard.utils import get_bounty, get_bounty_id, has_tx_mined, web3_process_bounty
from gas.utils import conf_time_spread, eth_usd_conv_rate, recommend_min_gas_price_to_confirm_in_time
from github.utils import (
    get_auth_url, get_github_emails, get_github_primary_email, get_github_user_data, is_github_token_valid,
)
from marketing.mails import bounty_uninterested
from marketing.models import Keyword
from ratelimit.decorators import ratelimit
from retail.helpers import get_ip
from web3 import HTTPProvider, Web3

logging.basicConfig(level=logging.DEBUG)

confirm_time_minutes_target = 4

# web3.py instance
w3 = Web3(HTTPProvider(settings.WEB3_HTTP_PROVIDER))


def send_tip(request):
    """Handle the first stage of sending a tip."""
    params = {
        'issueURL': request.GET.get('source'),
        'title': 'Send Tip',
        'class': 'send',
    }

    return TemplateResponse(request, 'yge/send1.html', params)


def record_user_action(profile_handle, event_name, instance):
    instance_class = instance.__class__.__name__.lower()

    try:
        user_profile = Profile.objects.filter(handle__iexact=profile_handle).first()
        UserAction.objects.create(
            profile=user_profile,
            action=event_name,
            metadata={
                f'{instance_class}_pk': instance.pk,
            })
    except Exception as e:
        # TODO: sync_profile?
        logging.error(f"error in record_action: {e} - {event_name} - {instance}")


def helper_handle_access_token(request, access_token):
    # https://gist.github.com/owocki/614a18fbfec7a5ed87c97d37de70b110
    # interest API via token
    github_user_data = get_github_user_data(access_token)
    request.session['handle'] = github_user_data['login']
    profile = Profile.objects.filter(handle__iexact=request.session['handle']).first()
    request.session['profile_id'] = profile.pk


@require_POST
@csrf_exempt
def new_interest(request, bounty_id):
    """Claim Work for a Bounty.

    :request method: POST

    Args:
        post_id (int): ID of the Bounty.

    Returns:
        dict: The success key with a boolean value and accompanying error.

    """
    access_token = request.GET.get('token')
    if access_token and is_github_token_valid(access_token):
        helper_handle_access_token(request, access_token)

    profile_id = request.session.get('profile_id')
    if not profile_id:
        return JsonResponse(
            {'error': 'You must be authenticated via github to use this feature!'},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        raise Http404

    num_issues = 3
    active_bounties = Bounty.objects.current().filter(idx_status__in=['open', 'started'])
    num_active = Interest.objects.filter(profile_id=profile_id, bounty__in=active_bounties).count()
    is_working_on_too_much_stuff = num_active >= num_issues
    if is_working_on_too_much_stuff:
        return JsonResponse({
            'error': f'You may only work on max of {num_issues} issues at once.',
            'success': False},
            status=401)

    try:
        Interest.objects.get(profile_id=profile_id, bounty=bounty)
        return JsonResponse({
            'error': 'You have already expressed interest in this bounty!',
            'success': False},
            status=401)
    except Interest.DoesNotExist:
        interest = Interest.objects.create(profile_id=profile_id)
        bounty.interested.add(interest)
        record_user_action(Profile.objects.get(pk=profile_id).handle, 'start_work', interest)
        maybe_market_to_slack(bounty, 'start_work')
        maybe_market_to_twitter(bounty, 'start_work')
    except Interest.MultipleObjectsReturned:
        bounty_ids = bounty.interested \
            .filter(profile_id=profile_id) \
            .values_list('id', flat=True) \
            .order_by('-created')[1:]

        Interest.objects.filter(pk__in=list(bounty_ids)).delete()

        return JsonResponse({
            'error': 'You have already expressed interest in this bounty!',
            'success': False},
            status=401)

    return JsonResponse({'success': True, 'profile': ProfileSerializer(interest.profile).data})


@require_POST
@csrf_exempt
def remove_interest(request, bounty_id):
    """Unclaim work from the Bounty.

    :request method: POST

    post_id (int): ID of the Bounty.

    Returns:
        dict: The success key with a boolean value and accompanying error.

    """
    access_token = request.GET.get('token')
    if access_token and is_github_token_valid(access_token):
        helper_handle_access_token(request, access_token)

    profile_id = request.session.get('profile_id')
    if not profile_id:
        return JsonResponse(
            {'error': 'You must be authenticated via github to use this feature!'},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)

    try:
        interest = Interest.objects.get(profile_id=profile_id, bounty=bounty)
        record_user_action(Profile.objects.get(pk=profile_id).handle, 'stop_work', interest)
        bounty.interested.remove(interest)
        interest.delete()
        maybe_market_to_slack(bounty, 'stop_work')
        maybe_market_to_twitter(bounty, 'stop_work')
    except Interest.DoesNotExist:
        return JsonResponse({
            'errors': ['You haven\'t expressed interest on this bounty.'],
            'success': False},
            status=401)
    except Interest.MultipleObjectsReturned:
        interest_ids = bounty.interested \
            .filter(
                profile_id=profile_id,
                bounty=bounty
            ).values_list('id', flat=True) \
            .order_by('-created')

        bounty.interested.remove(*interest_ids)
        Interest.objects.filter(pk__in=list(interest_ids)).delete()

    return JsonResponse({'success': True})


@require_POST
@csrf_exempt
def uninterested(request, bounty_id, profile_id):
    """Remove party from given bounty

    :request method: GET

    Args:
        bounty_id (int): ID of the Bounty
        profile_id (int): ID of the interested profile

    Returns:
        dict: The success key with a boolean value and accompanying error.
    """

    session_profile_id = request.session.get('profile_id')
    if not session_profile_id:
        return JsonResponse(
            {'error': 'You must be authenticated!'},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)

    if not bounty.is_funder(request.session.get('handle').lower()):
        return JsonResponse(
            {'error': 'Only bounty funders are allowed to remove users!'},
            status=401)

    try:
        interest = Interest.objects.get(profile_id=profile_id, bounty=bounty)
        bounty.interested.remove(interest)
        maybe_market_to_slack(bounty, 'stop_work')
        interest.delete()
    except Interest.DoesNotExist:
        return JsonResponse({
            'errors': ['Party haven\'t expressed interest on this bounty.'],
            'success': False},
            status=401)
    except Interest.MultipleObjectsReturned:
        interest_ids = bounty.interested \
            .filter(
                profile_id=profile_id,
                bounty=bounty
            ).values_list('id', flat=True) \
            .order_by('-created')

        bounty.interested.remove(*interest_ids)
        Interest.objects.filter(pk__in=list(interest_ids)).delete()

    profile = Profile.objects.get(id=profile_id)
    bounty_uninterested(profile.email, bounty, interest)
    return JsonResponse({'success': True})


@csrf_exempt
@ratelimit(key='ip', rate='2/m', method=ratelimit.UNSAFE, block=True)
def receive_tip(request):
    """Receive a tip."""
    if request.body:
        status = 'OK'
        message = 'Tip has been received'
        params = json.loads(request.body)

        # db mutations
        try:
            tip = Tip.objects.get(txid=params['txid'])
            tip.receive_address = params['receive_address']
            tip.receive_txid = params['receive_txid']
            tip.received_on = timezone.now()
            tip.save()
            record_user_action(tip.username, 'receive_tip', tip)
        except Exception as e:
            status = 'error'
            message = str(e)

        # http response
        response = {
            'status': status,
            'message': message,
        }

        return JsonResponse(response)

    params = {
        'issueURL': request.GET.get('source'),
        'class': 'receive',
        'title': 'Receive Tip',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
    }

    return TemplateResponse(request, 'yge/receive.html', params)


@csrf_exempt
@ratelimit(key='ip', rate='1/m', method=ratelimit.UNSAFE, block=True)
def send_tip_2(request):
    """Handle the second stage of sending a tip.

    TODO:
        * Convert this view-based logic to a django form.

    Returns:
        JsonResponse: If submitting tip, return response with success state.
        TemplateResponse: Render the submission form.

    """
    from_username = request.session.get('handle', '')
    primary_from_email = request.session.get('email', '')
    access_token = request.session.get('access_token')
    to_emails = []

    if request.body:
        # http response
        response = {
            'status': 'OK',
            'message': 'Notification has been sent',
        }
        params = json.loads(request.body)

        to_username = params['username'].lstrip('@')
        try:
            to_profile = Profile.objects.get(handle__iexact=to_username)
            if to_profile.email:
                to_emails.append(to_profile.email)
            if to_profile.github_access_token:
                to_emails = get_github_emails(to_profile.github_access_token)
        except Profile.DoesNotExist:
            pass

        if params.get('email'):
            to_emails.append(params['email'])

        # If no primary email in session, try the POST data. If none, fetch from GH.
        if params.get('fromEmail'):
            primary_from_email = params['fromEmail']
        elif access_token and not primary_from_email:
            primary_from_email = get_github_primary_email(access_token)

        to_emails = list(set(to_emails))
        expires_date = timezone.now() + timezone.timedelta(seconds=params['expires_date'])

        # db mutations
        tip = Tip.objects.create(
            emails=to_emails,
            url=params['url'],
            tokenName=params['tokenName'],
            amount=params['amount'],
            comments_priv=params['comments_priv'],
            comments_public=params['comments_public'],
            ip=get_ip(request),
            expires_date=expires_date,
            github_url=params['github_url'],
            from_name=params['from_name'],
            from_email=params['from_email'],
            from_username=from_username,
            username=params['username'],
            network=params['network'],
            tokenAddress=params['tokenAddress'],
            txid=params['txid'],
            from_address=params['from_address'],
        )
        # notifications
        maybe_market_tip_to_github(tip)
        maybe_market_tip_to_slack(tip, 'new_tip')
        maybe_market_tip_to_email(tip, to_emails)
        record_user_action(tip.username, 'send_tip', tip)
        if not to_emails:
            response['status'] = 'error'
            response['message'] = 'Uh oh! No email addresses for this user were found via Github API.  Youll have to let the tipee know manually about their tip.'

        return JsonResponse(response)

    params = {
        'issueURL': request.GET.get('source'),
        'class': 'send2',
        'title': 'Send Tip',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'from_email': primary_from_email,
        'from_handle': from_username,
    }

    return TemplateResponse(request, 'yge/send2.html', params)


def process_bounty(request):
    """Process the bounty."""
    params = {
        'issueURL': request.GET.get('source'),
        'fulfillment_id': request.GET.get('id'),
        'fulfiller_address': request.GET.get('address'),
        'title': 'Process Issue',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'eth_usd_conv_rate': eth_usd_conv_rate(),
        'conf_time_spread': conf_time_spread(),
    }

    return TemplateResponse(request, 'process_bounty.html', params)


def dashboard(request):
    """Handle displaying the dashboard."""
    params = {
        'active': 'dashboard',
        'title': 'Issue Explorer',
        'keywords': json.dumps([str(key) for key in Keyword.objects.all().values_list('keyword', flat=True)]),
    }
    return TemplateResponse(request, 'dashboard.html', params)


def gas(request):
    context = {
        'conf_time_spread': conf_time_spread(),
        'title': 'Live Gas Usage => Predicted Conf Times'
        }
    return TemplateResponse(request, 'gas.html', context)


def new_bounty(request):
    """Create a new bounty."""
    issue_url = request.GET.get('source') or request.GET.get('url', '')
    params = {
        'issueURL': issue_url,
        'amount': request.GET.get('amount'),
        'active': 'submit_bounty',
        'title': 'Create Funded Issue',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'eth_usd_conv_rate': eth_usd_conv_rate(),
        'conf_time_spread': conf_time_spread(),
        'from_email': request.session.get('email', ''),
        'from_handle': request.session.get('handle', ''),
        'newsletter_headline': 'Be the first to know about new funded issues.'
    }

    return TemplateResponse(request, 'submit_bounty.html', params)


def fulfill_bounty(request):
    """Fulfill a bounty."""
    params = {
        'issueURL': request.GET.get('source'),
        'githubUsername': request.GET.get('githubUsername'),
        'title': 'Submit Work',
        'active': 'fulfill_bounty',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'eth_usd_conv_rate': eth_usd_conv_rate(),
        'conf_time_spread': conf_time_spread(),
        'handle': request.session.get('handle', ''),
        'email': request.session.get('email', '')
    }

    return TemplateResponse(request, 'fulfill_bounty.html', params)


def increase_bounty(request):
    """Increase a bounty (funder)"""
    issue_url = request.GET.get('source')
    params = {
        'issue_url': issue_url,
        'title': 'Increase Bounty',
        'active': 'increase_bounty',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'eth_usd_conv_rate': eth_usd_conv_rate(),
        'conf_time_spread': conf_time_spread(),
    }

    try:
        bounties = Bounty.objects.current().filter(github_url=issue_url)
        if bounties:
            bounty = bounties.order_by('pk').first()
            params['standard_bounties_id'] = bounty.standard_bounties_id
            params['bounty_owner_address'] = bounty.bounty_owner_address
            params['value_in_token'] = bounty.value_in_token
            params['token_address'] = bounty.token_address
    except Bounty.DoesNotExist:
        pass
    except Exception as e:
        print(e)
        logging.error(e)

    return TemplateResponse(request, 'increase_bounty.html', params)


def kill_bounty(request):
    """Kill an expired bounty."""
    params = {
        'issueURL': request.GET.get('source'),
        'title': 'Kill Bounty',
        'active': 'kill_bounty',
        'recommend_gas_price': recommend_min_gas_price_to_confirm_in_time(confirm_time_minutes_target),
        'eth_usd_conv_rate': eth_usd_conv_rate(),
        'conf_time_spread': conf_time_spread(),
    }

    return TemplateResponse(request, 'kill_bounty.html', params)


def bounty_details(request, ghuser='', ghrepo='', ghissue=0):
    """Display the bounty details."""
    _access_token = request.session.get('access_token')
    profile_id = request.session.get('profile_id')
    issueURL = 'https://github.com/' + ghuser + '/' + ghrepo + '/issues/' + ghissue if ghissue else request.GET.get('url')

    # try the /pulls url if it doesnt exist in /issues
    try:
        assert Bounty.objects.current().filter(github_url=issueURL).exists()
    except Exception:
        issueURL = 'https://github.com/' + ghuser + '/' + ghrepo + '/pull/' + ghissue if ghissue else request.GET.get('url')
        print(issueURL)

    bounty_url = issueURL
    params = {
        'issueURL': issueURL,
        'title': 'Issue Details',
        'card_title': 'Funded Issue Details | Gitcoin',
        'avatar_url': static('v2/images/helmet.png'),
        'active': 'bounty_details',
        'is_github_token_valid': is_github_token_valid(_access_token),
        'github_auth_url': get_auth_url(request.path),
        'profile_interested': False,
        "newsletter_headline": "Be the first to know about new funded issues."
    }

    if bounty_url:
        try:
            bounties = Bounty.objects.current().filter(github_url=bounty_url)
            if bounties:
                bounty = bounties.order_by('pk').first()
                # Currently its not finding anyting in the database
                if bounty.title and bounty.org_name:
                    params['card_title'] = f'{bounty.title} | {bounty.org_name} Funded Issue Detail | Gitcoin'
                    params['title'] = params['card_title']
                    params['card_desc'] = ellipses(bounty.issue_description_text, 255)

                params['bounty_pk'] = bounty.pk
                params['interested_profiles'] = bounty.interested.select_related('profile').all()
                params['avatar_url'] = bounty.local_avatar_url
                if profile_id:
                    profile_ids = list(params['interested_profiles'].values_list('profile_id', flat=True))
                    params['profile_interested'] = request.session.get('profile_id') in profile_ids
        except Bounty.DoesNotExist:
            pass
        except Exception as e:
            print(e)
            logging.error(e)

    return TemplateResponse(request, 'bounty_details.html', params)


def profile_helper(handle):
    """Define the profile helper."""
    try:
        profile = Profile.objects.get(handle__iexact=handle)
    except Profile.DoesNotExist:
        profile = sync_profile(handle)
        if not profile:
            raise Http404
    except Profile.MultipleObjectsReturned as e:
        # Handle edge case where multiple Profile objects exist for the same handle.
        # We should consider setting Profile.handle to unique.
        # TODO: Should we handle merging or removing duplicate profiles?
        profile = Profile.objects.filter(handle__iexact=handle).latest('id')
        logging.error(e)
    return profile


def profile_keywords_helper(handle):
    """Define the profile keywords helper."""
    profile = profile_helper(handle)

    keywords = []
    for repo in profile.repos_data:
        language = repo.get('language') if repo.get('language') else ''
        _keywords = language.split(',')
        for key in _keywords:
            if key != '' and key not in keywords:
                keywords.append(key)
    return keywords


def profile_keywords(request, handle):
    """Display profile keywords."""
    keywords = profile_keywords_helper(handle)

    response = {
        'status': 200,
        'keywords': keywords,
    }
    return JsonResponse(response)


def profile(request, handle):
    """Display profile details."""
    handle = handle or request.session.get('handle')

    if not handle:
        raise Http404

    params = {
        'title': 'Profile',
        'active': 'profile_details',
        'newsletter_headline': 'Be the first to know about new funded issues.',
    }

    profile = profile_helper(handle)
    params['card_title'] = f"@{handle} | Gitcoin"
    params['card_desc'] = profile.desc
    params['title'] = f"@{handle}"
    params['avatar_url'] = profile.local_avatar_url
    params['profile'] = profile
    params['stats'] = profile.stats
    params['bounties'] = profile.bounties
    params['tips'] = Tip.objects.filter(username=handle, network='mainnet')

    return TemplateResponse(request, 'profile_details.html', params)


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def save_search(request):
    """Save the search."""
    email = request.POST.get('email')
    if email:
        raw_data = request.POST.get('raw_data')
        Subscription.objects.create(
            email=email,
            raw_data=raw_data,
            ip=get_ip(request),
        )
        response = {
            'status': 200,
            'msg': 'Success!',
        }
        return JsonResponse(response)

    context = {
        'active': 'save',
        'title': 'Save Search',
    }
    return TemplateResponse(request, 'save_search.html', context)


@require_POST
@csrf_exempt
@ratelimit(key='ip', rate='5/s', method=ratelimit.UNSAFE, block=True)
def sync_web3(request):
    """ Sync up web3 with the database.  This function has a few different uses.  It is typically
        called from the front end using the javascript `sync_web3` function.  The `issueURL` is
        passed in first, followed optionally by a `bountydetails` argument.
    """
    # setup
    result = {
        'status': '400',
        'msg': "bad request"
    }

    issue_url = request.POST.get('url')
    txid = request.POST.get('txid')
    network = request.POST.get('network')

    if issue_url and txid and network:
        # confirm txid has mined
        print('* confirming tx has mined')
        if not has_tx_mined(txid, network):
            result = {
                'status': '400',
                'msg': 'tx has not mined yet'
            }
        else:

            # get bounty id
            print('* getting bounty id')
            bounty_id = get_bounty_id(issue_url, network)
            if not bounty_id:
                result = {
                    'status': '400',
                    'msg': 'could not find bounty id'
                }
            else:
                # get/process bounty
                print('* getting bounty')
                bounty = get_bounty(bounty_id, network)
                print('* processing bounty')
                did_change = False
                max_tries_attempted = False
                counter = 0
                while not did_change and not max_tries_attempted:
                    did_change, _, _ = web3_process_bounty(bounty)
                    if not did_change:
                        print("RETRYING")
                        time.sleep(3)
                        counter += 1
                        max_tries_attempted = counter > 3
                result = {
                    'status': '200',
                    'msg': "success",
                    'did_change': did_change
                }

    return JsonResponse(result, status=result['status'])


# LEGAL

def terms(request):
    return TemplateResponse(request, 'legal/terms.txt', {})


def privacy(request):
    return redirect('https://gitcoin.co/terms#privacy')


def cookie(request):
    return redirect('https://gitcoin.co/terms#privacy')


def prirp(request):
    return redirect('https://gitcoin.co/terms#privacy')


def apitos(request):
    return redirect('https://gitcoin.co/terms#privacy')


def toolbox(request):
    access_token = request.GET.get('token')    
    if access_token and is_github_token_valid(access_token):
        helper_handle_access_token(request, access_token)

    basic_tools = Tool.objects.filter(category='BASIC')
    advanced_tools = Tool.objects.filter(category='ADVANCED')
    community_tools = Tool.objects.filter(category='COMMUNITY')
    to_build_toos = Tool.objects.filter(category='TOOLS_TO_BUILD')
    alpha_tools = Tool.objects.filter(category='ALPHA')
    coming_soon_tools = Tool.objects.filter(category='COMING_SOON')
    for_fun_tools = Tool.objects.filter(category='FOR_FUN')
    actors = [{
        "title": "Basics",
        "description": "Accelerate your dev workflow with Gitcoin\'s incentivization tools.",
        "tools": basic_tools
      }, {
          "title": "Advanced",
          "description": "Take your OSS game to the next level!",
          "tools": advanced_tools
      }, {
          "title": "Community",
          "description": "Friendship, mentorship, and community are all part of the process.",
          "tools": community_tools
       }, {
          "title": "Tools to BUIDL Gitcoin",
          "description": "Gitcoin is built using Gitcoin.  Purdy cool, huh? ",
          "tools": to_build_toos
       }, {
          "title": "Tools in Alpha",
          "description": "These fresh new tools are looking for someone to test ride them!",
          "tools": alpha_tools
       }, {
           "title": "Tools Coming Soon",
           "description": "These tools will be ready soon.  They'll get here sooner if you help BUIDL them :)",
           "tools": coming_soon_tools
       }, {
           "title": "Just for Fun",
           "description": "Some tools that the community built *just because* they should exist.",
           "tools": for_fun_tools
        }
        ]

    # setup slug
    for key in range(0, len(actors)):
        actors[key]['slug'] = slugify(actors[key]['title'])

    profile_id = request.session.get('profile_id')
    ups = list(ToolVote.objects.filter(profile_id = profile_id, value = 1).values_list('tool', flat=True))
    profile_up_votes_tool_ids = ','.join(str(x) for x in ups)
    downs = list(ToolVote.objects.filter(profile_id = profile_id, value = -1).values_list('tool', flat=True))
    profile_down_votes_tool_ids = ','.join(str(x) for x in downs)
    context = {
        "active": "tools",
        'title': "Toolbox",
        'card_title': "Gitcoin Toolbox",
        'avatar_url': static('v2/images/tools/api.jpg'),
        "card_desc": "Accelerate your dev workflow with Gitcoin\'s incentivization tools.",
        'actors': actors,
        'newsletter_headline': "Don't Miss New Tools!",
        'profile_up_votes_tool_ids': profile_up_votes_tool_ids,
        'profile_down_votes_tool_ids': profile_down_votes_tool_ids
    }
    return TemplateResponse(request, 'toolbox.html', context)

@csrf_exempt
@require_POST
def vote_tool_up(request, tool_id):

    access_token = request.GET.get('token')    
    if access_token and is_github_token_valid(access_token):
        helper_handle_access_token(request, access_token)

    tool = Tool.objects.get(pk=tool_id)    

    profile_id = request.session.get('profile_id')
    if not profile_id:
        return JsonResponse(
            {'error': 'You must be authenticated via github to use this feature!'},
            status=401)

    try:
        ToolVote.objects.get(profile_id=profile_id, tool=tool)
        return JsonResponse({
            'error': 'You have already voted on this tool!',
            'success': False},
            status=401)
    except ToolVote.DoesNotExist:
        vote = ToolVote.objects.create(profile_id=profile_id, value=1)
        tool.votes.add(vote)        
    return JsonResponse({'success': True})   

@csrf_exempt
@require_POST
def vote_tool_down(request, tool_id):

    access_token = request.GET.get('token')    
    if access_token and is_github_token_valid(access_token):
        helper_handle_access_token(request, access_token)

    tool = Tool.objects.get(pk=tool_id)    

    profile_id = request.session.get('profile_id')
    if not profile_id:
        return JsonResponse(
            {'error': 'You must be authenticated via github to use this feature!'},
            status=401)

    try:
        ToolVote.objects.get(profile_id=profile_id, tool=tool)
        return JsonResponse({
            'error': 'You have already voted on this tool!',
            'success': False},
            status=401)
    except ToolVote.DoesNotExist:
        vote = ToolVote.objects.create(profile_id=profile_id, value=-1)
        tool.votes.add(vote)        
    return JsonResponse({'success': True})   

@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def redeem_coin(request, shortcode):
    if request.body:
        status = 'OK'

        body_unicode = request.body.decode('utf-8')
        body = json.loads(body_unicode)
        address = body['address']

        try:
            coin = CoinRedemption.objects.get(shortcode=shortcode)
            address = Web3.toChecksumAddress(address)

            if hasattr(coin, 'coinredemptionrequest'):
                status = 'error'
                message = 'Bad request'
            else:
                abi = json.loads('[{"constant":true,"inputs":[],"name":"mintingFinished","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_from","type":"address"},{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transferFrom","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"name":"mint","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"version","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_subtractedValue","type":"uint256"}],"name":"decreaseApproval","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[],"name":"finishMinting","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"owner","outputs":[{"name":"","type":"address"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_addedValue","type":"uint256"}],"name":"increaseApproval","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"newOwner","type":"address"}],"name":"transferOwnership","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"payable":false,"stateMutability":"nonpayable","type":"fallback"},{"anonymous":false,"inputs":[{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"amount","type":"uint256"}],"name":"Mint","type":"event"},{"anonymous":false,"inputs":[],"name":"MintFinished","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"previousOwner","type":"address"},{"indexed":true,"name":"newOwner","type":"address"}],"name":"OwnershipTransferred","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"owner","type":"address"},{"indexed":true,"name":"spender","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Approval","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"}]')

                # Instantiate Colorado Coin contract
                contract = w3.eth.contract(coin.contract_address, abi=abi)

                tx = contract.functions.transfer(address, coin.amount * 10**18).buildTransaction({
                    'nonce': w3.eth.getTransactionCount(settings.COLO_ACCOUNT_ADDRESS),
                    'gas': 100000,
                    'gasPrice': recommend_min_gas_price_to_confirm_in_time(5) * 10**9
                })

                signed = w3.eth.account.signTransaction(tx, settings.COLO_ACCOUNT_PRIVATE_KEY)
                transaction_id = w3.eth.sendRawTransaction(signed.rawTransaction).hex()

                CoinRedemptionRequest.objects.create(
                    coin_redemption=coin,
                    ip=get_ip(request),
                    sent_on=timezone.now(),
                    txid=transaction_id,
                    txaddress=address
                )

                message = transaction_id
        except CoinRedemption.DoesNotExist:
            status = 'error'
            message = 'Bad request'
        except Exception as e:
            status = 'error'
            message = str(e)

        # http response
        response = {
            'status': status,
            'message': message,
        }

        return JsonResponse(response)

    try:
        coin = CoinRedemption.objects.get(shortcode=shortcode)

        params = {
            'class': 'redeem',
            'title': 'Coin Redemption',
            'coin_status': 'PENDING'
        }

        try:
            coin_redeem_request = CoinRedemptionRequest.objects.get(coin_redemption=coin)
            params['colo_txid'] = coin_redeem_request.txid
        except CoinRedemptionRequest.DoesNotExist:
            params['coin_status'] = 'INITIAL'

        return TemplateResponse(request, 'yge/redeem_coin.html', params)
    except CoinRedemption.DoesNotExist:
        raise Http404
