#!/usr/bin/env python
# -*- coding: utf-8 -*-


# =============================================================================
# IMPORTS
# =============================================================================

import threading
import time
import vanilla

import django.utils.timezone
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.contrib import messages
from django.http import (
    HttpResponse, HttpResponseRedirect, HttpResponseNotFound
)

import otree.constants as constants
import otree.models.session
import otree.views.admin
import otree.common_internal
from otree.views.abstract import (
    NonSequenceUrlMixin, OTreeMixin, AssignVisitorToOpenSessionBase,
    GenericWaitPageMixin, FormPageOrWaitPageMixin, PlayerMixin
)


class OutOfRangeNotification(NonSequenceUrlMixin, OTreeMixin, vanilla.View):
    name_in_url = 'shared'

    def dispatch(self, request, *args, **kwargs):
        user_type = kwargs.pop(constants.user_type)
        if user_type == constants.user_type_experimenter:
            return TemplateResponse(
                request, 'otree/OutOfRangeNotificationExperimenter.html'
            )
        else:
            return TemplateResponse(
                request, 'otree/OutOfRangeNotification.html'
            )


class WaitUntilAssignedToGroup(FormPageOrWaitPageMixin, PlayerMixin,
                               GenericWaitPageMixin, vanilla.View):
    """This is visited after Initialize, to make sure the player has a group
    the player can be assigned at any time, but this is a safeguard,
    and therefore should be at the beginning of each subsession.
    Should it instead be called after InitializeParticipant?
    Someday, we might want to shuffle players dynamically,
    e.g. based on the results of the past game.
    """
    name_in_url = 'shared'

    def _is_ready(self):
        return bool(self.group)

    def body_text(self):
        return (
            'Waiting until other participants and/or '
            'the study supervisor are ready.'
        )

    def _response_when_ready(self):
        self._increment_index_in_pages()
        # so it can be shown in the admin
        self._session_user._round_number = self.subsession.round_number
        return self._redirect_to_page_the_user_should_be_on()

    def get_debug_values(self):
        pass


class SessionExperimenterWaitUntilPlayersAreAssigned(NonSequenceUrlMixin,
                                                     GenericWaitPageMixin,
                                                     vanilla.View):

    def title_text(self):
        return 'Please wait'

    def body_text(self):
        return 'Assigning players to groups.'

    def _is_ready(self):
        return self.session._players_assigned_to_groups

    @classmethod
    def get_name_in_url(cls):
        return 'shared'

    def dispatch(self, request, *args, **kwargs):
        session_user_code = kwargs[constants.session_user_code]
        self.request.session[session_user_code] = {}

        self._session_user = get_object_or_404(
            otree.models.session.SessionExperimenter,
            code=kwargs[constants.session_user_code]
        )

        self.session = self._session_user.session

        if self.request_is_from_wait_page():
            return self._response_to_wait_page()
        else:
            # if the player shouldn't see this view, skip to the next
            if self._is_ready():
                # FIXME 2014-12-4: what should this do instead of directing
                # to the start url?
                return HttpResponse(
                    'not yet implemented: redirect to experimenter page'
                )
            return self._get_wait_page()


class InitializeSessionExperimenter(vanilla.View):

    @classmethod
    def url_pattern(cls):
        return r'^InitializeSessionExperimenter/(?P<{}>[a-z]+)/$'.format(
            constants.session_user_code
        )

    def redirect_to_next_page(self):
        url = SessionExperimenterWaitUntilPlayersAreAssigned.url(
            self._session_user
        )
        return HttpResponseRedirect(url)

    def get(self, *args, **kwargs):

        self._session_user = get_object_or_404(
            otree.models.session.SessionExperimenter,
            code=kwargs[constants.session_user_code]
        )

        session = self._session_user.session
        if session._players_assigned_to_groups:
            return self.redirect_to_next_page()
        return TemplateResponse(
            self.request, 'otree/experimenter/StartSession.html', {}
        )

    def post(self, request, *args, **kwargs):
        self._session_user = get_object_or_404(
            otree.models.session.SessionExperimenter,
            code=kwargs[constants.session_user_code]
        )

        session = self._session_user.session

        if not session.time_started:
            # get timestamp when the experimenter starts, rather than when the
            # session was created (since code is often updated after session
            # created)
            session.git_commit_timestamp = (
                otree.common_internal.git_commit_timestamp()
            )
            session.time_started = django.utils.timezone.now()
            session.save()

        t = threading.Thread(target=session._create_groups_and_initialize())
        t.start()
        return self.redirect_to_next_page()


class InitializeParticipant(vanilla.UpdateView):
    """just collects data and sets properties. not essential to functionality.
    the only exception is if the participant needs to be assigned to groups on
    the fly, which is done here.

    2014-11-16: also, this sets _last_page_timestamp. what if that is not set?
    will it still work?

    """

    @classmethod
    def url_pattern(cls):
        return r'^InitializeParticipant/(?P<{}>[a-z]+)/$'.format(
            constants.session_user_code
        )

    def get(self, *args, **kwargs):

        session_user = get_object_or_404(
            otree.models.session.Participant,
            code=kwargs[constants.session_user_code]
        )

        session = session_user.session
        cond = (
            (not session_user.visited) and
            session.session_type.group_by_arrival_time
        )
        if cond:
            next_participant = session._next_participant_to_assign()
            if next_participant:
                session._swap_participant_codes(session_user, next_participant)
                session_user = next_participant

        session_user.visited = True

        # session_user.label might already have been set by AssignToOpenSession
        session_user.label = session_user.label or self.request.GET.get(
            constants.participant_label
        )
        session_user.ip_address = self.request.META['REMOTE_ADDR']

        now = django.utils.timezone.now()
        session_user.time_started = now
        session_user._last_page_timestamp = time.time()
        session_user.save()
        first_url = session_user._pages_as_urls()[session_user._index_in_pages]
        return HttpResponseRedirect(first_url)


class AssignVisitorToOpenSessionMTurk(AssignVisitorToOpenSessionBase):

    def incorrect_parameters_in_url_message(self):
        # A visitor to this experiment was turned away because they did not
        # have the MTurk parameters in their URL. This URL only works if
        # clicked from a MTurk job posting with the JavaScript snippet embedded
        return ("To participate, you need to first accept this Mechanical "
                "Turk HIT and then re-click the link (refreshing this page "
                "will not work).")

    @classmethod
    def url(cls):
        return otree.common_internal.add_params_to_url(
            '/{}'.format(cls.__name__), {
                otree.constants.access_code_for_open_session:
                    settings.ACCESS_CODE_FOR_OPEN_SESSION
            }
        )

    @classmethod
    def url_pattern(cls):
        return r'^{}/$'.format(cls.__name__)

    required_params = {
        'mturk_worker_id': otree.constants.mturk_worker_id,
        'mturk_assignment_id': otree.constants.mturk_assignment_id,
    }

    def url_has_correct_parameters(self):
        return (
            super(
                AssignVisitorToOpenSessionMTurk, self
            ).url_has_correct_parameters() and
            self.request.GET[
                constants.mturk_assignment_id
            ] != 'ASSIGNMENT_ID_NOT_AVAILABLE'
        )


class AssignVisitorToOpenSession(AssignVisitorToOpenSessionBase):

    def incorrect_parameters_in_url_message(self):
        return 'Missing parameter(s) in URL: {}'.format(
            self.required_params.values()
        )

    @classmethod
    def url(cls):
        return otree.common_internal.add_params_to_url(
            '/{}'.format(cls.__name__), {
                otree.constants.access_code_for_open_session:
                    settings.ACCESS_CODE_FOR_OPEN_SESSION
            }
        )

    @classmethod
    def url_pattern(cls):
        return r'^{}/$'.format(cls.__name__)

    required_params = {
        'label': otree.constants.participant_label,
    }


class AdvanceSession(vanilla.View):

    @classmethod
    def url_pattern(cls):
        return r'^AdvanceSession/(?P<{}>[0-9]+)/(?P<{}>[a-z]+)/$'.format(
            'session_pk', constants.admin_access_code
        )

    @classmethod
    def url(cls, session):
        gs = otree.models.session.GlobalSingleton.objects.get()
        return '/AdvanceSession/{}/{}/'.format(
            session.pk, gs.admin_access_code
        )

    def dispatch(self, request, *args, **kwargs):
        gs = otree.models.session.GlobalSingleton.objects.get()
        if not kwargs.get(constants.admin_access_code) == gs.admin_access_code:
            return HttpResponseNotFound(
                'incorrect or missing admin access code'
            )
        self.session = get_object_or_404(
            otree.models.session.Session, pk=kwargs['session_pk']
        )
        response = super(AdvanceSession, self).dispatch(
            request, *args, **kwargs
        )
        messages.success(request, "Participants were advanced.")
        return response

    def get(self, request, *args, **kwargs):
        self.session.advance_last_place_participants()
        admin_url = otree.views.admin.SessionHome.url(self.session.pk)
        return HttpResponseRedirect(admin_url)
