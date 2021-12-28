##############################################################################
#
#    GNU Condo: The Free Management Condominium System
#    Copyright (C) 2016- M. Alonso <port02.server@gmail.com>
#
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

import datetime
import logging
import sys
import traceback
from ast import literal_eval
from email.header import Header
from email.mime.text import MIMEText

from dateutil import rrule
from dateutil.relativedelta import relativedelta, weekdays
from sql import Null
from trytond.config import config
from trytond.model import (
    Check,
    DeactivableMixin,
    ModelSQL,
    ModelView,
    Unique,
    dualmethod,
    fields,
)
from trytond.modules.holidays.calendar import _weekday_map
from trytond.modules.holidays.calendar import handle_byweekday_item as handle_weekday
from trytond.pool import Pool
from trytond.sendmail import sendmail
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction

__all__ = ['Recurrence', 'RecurrenceDate', 'RecurrenceEvent']

logger = logging.getLogger(__name__)


class Recurrence(DeactivableMixin, ModelSQL, ModelView):
    'Recurrence Rule'
    __name__ = 'recurrence'

    name = fields.Char('Name', required=True)
    description = fields.Text('Description')

    years = fields.Integer('Years')
    months = fields.Integer('Months')
    weeks = fields.Integer('Weeks')
    days = fields.Integer('Days')

    weekday = fields.Char(
        'Weekday',
        help=(
            'One of the weekday instances (MO, TU, etc) '
            'and an optional parameter N (positive or negative), '
            'specifying the Nth weekday like MO(-2).'
            ' You can also use an integer where 0=MO'
        ),
    )
    leapdays = fields.Integer(
        'Leapdays',
        help=(
            'Will add given days to the date found if year is a leap' ' year and the date found is post 28 of february'
        ),
    )

    dtstart = fields.DateTime('Start', required=True, select=True)
    next_call = fields.Function(fields.DateTime('Next Call'), getter='get_next_call')
    direction = fields.Selection(
        [('after', 'After'), ('before', 'Before')],
        'Direction',
        required=True,
        help=('Movement direction in time to find ' 'recurrence date when loop fall into excluded date'),
    )
    events = fields.One2Many('recurrence.event', 'recurrence', 'Events')

    @classmethod
    def __setup__(cls):
        super(Recurrence, cls).__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            (
                'at_least_one_interval',
                Check(t, (t.years != Null) | (t.months != Null) | (t.weeks != Null) | (t.days != Null)),
                'At least one frequency interval must be set.',
            ),
            ('name_uniq', Unique(t, t.name), 'The name of recurrence must be unique.'),
        ]
        cls._error_messages.update({'invalid_weekday': 'Invalid "Weekday" in recurrence "%s"'})

    def get_next_call(self, name, now=None):
        if not self.active:
            return None
        if not now:
            now = datetime.datetime.now()

        delta, dtend, n = 1, self.dtstart, 1
        while dtend < now and delta > 0:
            dt = dtend
            dtend = self.dtstart + self.get_delta(n=n)
            delta = (dtend - dt).days
            n += 1
        return dtend

    @staticmethod
    def default_dtstart():
        return datetime.datetime.now()

    @staticmethod
    def default_direction():
        return 'after'

    @classmethod
    def validate(cls, recurrences):
        super(Recurrence, cls).validate(recurrences)
        for recurrence in recurrences:
            recurrence.check_weekday()

    def check_weekday(self):
        if self.weekday:
            n = w = None
            try:
                if '(' in self.weekday:
                    # If it's of the form TH(+1), etc.
                    splt = self.weekday.split('(')
                    w = splt[0]
                    n = int(splt[1][:-1])
                elif len(self.weekday):
                    # If it's of the form +1MO
                    for i in range(len(self.weekday)):
                        if self.weekday[i] not in '+-0123456789':
                            n = self.weekday[:i] or None
                            w = self.weekday[i:]
                            break
                        n = self.weekday[: i + 1] or None
                    if n:
                        n = int(n)
            except Exception:
                self.raise_user_error('invalid_weekday', (self.rec_name,))
            if (w and w.strip() not in ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']) or (not w and n and abs(n) > 6):
                self.raise_user_error('invalid_weekday', (self.rec_name,))

    def get_delta(self, n=1):
        '''
        Return the relativedelta for the next call
        '''
        try:
            i = int(n)
        except TypeError:
            return NotImplemented

        return relativedelta(
            years=self.years * i if self.years else 0,
            months=self.months * i if self.months else 0,
            weeks=self.weeks * i if self.weeks else 0,
            days=self.days * i if self.days else 0,
            weekday=handle_weekday(self.weekday),
            leapdays=self.leapdays if self.leapdays else 0,
        )

    @classmethod
    def write(cls, recurrences, values, *args):

        events = Pool().get('recurrence.event').__table__()
        cursor = Transaction().connection.cursor()

        kwrd = {}
        actions = iter((recurrences, values) + args)
        for records, values in zip(actions, actions):
            for r in records:
                kwrd[r.id] = r.next_call

        super(Recurrence, cls).write(recurrences, values, *args)

        actions = iter((recurrences, values) + args)
        for records, values in zip(actions, actions):
            for r in records:
                if kwrd[r.id] != r.next_call:
                    ids = [e.id for e in r.events]

                    if len(ids):
                        for sub_ids in grouped_slice(ids):
                            red_sql = reduce_ids(events.id, sub_ids)
                            cursor.execute(
                                *events.update(columns=[events.rnext_call], values=[r.next_call], where=red_sql)
                            )


class RecurrenceDate(ModelSQL, ModelView):
    'Recurrence Date'
    __name__ = 'recurrence.date'

    name = fields.Char('Name', select=True)
    event = fields.Many2One('recurrence.event', 'Event', required=True, select=True, ondelete='CASCADE')
    delta_days = fields.Integer('Delta Days', required=True, select=True)
    holidays = fields.Many2One('holidays.calendar', 'Holidays Calendar', select=True, ondelete='SET NULL')
    trigger = fields.Boolean('Trigger', select=True)
    date = fields.Function(fields.DateTime('Date'), getter='get_date')
    mo = fields.Boolean('Monday')
    tu = fields.Boolean('Tuesday')
    we = fields.Boolean('Wednesday')
    th = fields.Boolean('Thursday')
    fr = fields.Boolean('Friday')
    sa = fields.Boolean('Saturday')
    su = fields.Boolean('Sunday')

    @classmethod
    def __setup__(cls):
        super(RecurrenceDate, cls).__setup__()
        t = cls.__table__()
        cls._sql_constraints += [('name_uniq', Unique(t, t.name, t.event), 'The name of date must be unique.')]

    @staticmethod
    def default_trigger():
        return False

    @staticmethod
    def default_mo():
        return False

    @staticmethod
    def default_tu():
        return False

    @staticmethod
    def default_we():
        return False

    @staticmethod
    def default_th():
        return False

    @staticmethod
    def default_fr():
        return False

    @staticmethod
    def default_sa():
        return True

    @staticmethod
    def default_su():
        return True

    def get_date(self, name, dtstart=None, dtuntil=None, dt=None):
        if not dt:
            dt = self.event.rnext_call if self.event.rnext_call else self.event.recurrence.next_call

        if not dtstart:
            dtstart = max(dt - self.event.recurrence.get_delta(n=2), self.event.recurrence.dtstart).date()
        if not dtuntil:
            dtuntil = max(datetime.datetime.today(), dt).date() + self.event.recurrence.get_delta(n=2)

        byweekday = []

        rs = rrule.rruleset(cache=True)
        rs.rrule(rrule.rrule(rrule.DAILY, dtstart=dtstart, until=dtuntil))

        if self.mo:
            byweekday.append(rrule.MO)
        if self.tu:
            byweekday.append(rrule.TU)
        if self.tu:
            byweekday.append(rrule.WE)
        if self.th:
            byweekday.append(rrule.TH)
        if self.fr:
            byweekday.append(rrule.FR)
        if self.sa:
            byweekday.append(rrule.SA)
        if self.su:
            byweekday.append(rrule.SU)

        if len(byweekday):
            rs.exrule(rrule.rrule(rrule.WEEKLY, dtstart=dtstart, until=dtuntil, byweekday=byweekday))
        if self.holidays:
            rs.exrule(self.holidays.calendar2alldates(from_date=dtstart, to_date=dtuntil))

        type = 'after' if self.delta_days >= 0 else 'before'
        i = abs(self.delta_days)
        incr = (self.delta_days > 0) - (self.delta_days < 0)

        # find first date on direction time specified by recurrence
        dtnx = getattr(rs, self.event.recurrence.direction)(dt.replace(hour=0, minute=0, second=0), inc=True)

        # now got forward or backwards on time depending on self.delta_days
        while i and dtnx:
            dtnx += datetime.timedelta(days=incr)
            dtnx = getattr(rs, type)(dtnx, inc=True)
            i -= 1

        return dtnx.replace(hour=dt.hour, minute=dt.minute, second=dt.second)

    def update_event_rnext_call(self):
        event = self.event
        recurrence = event.recurrence

        now = datetime.datetime.now()

        # if event next_call points to past
        if event.next_call < now or event.rnext_call != recurrence.next_call:
            dt, n = recurrence.dtstart, 1

            # forward dt until is equal to recurrence next_call
            while dt < min(event.rnext_call or recurrence.next_call, recurrence.next_call):
                dt = recurrence.dtstart + recurrence.get_delta(n=n)
                n += 1

            # while next_call lower than now
            while self.get_date('date', dt=dt) < now:
                dt = recurrence.dtstart + recurrence.get_delta(n=n)
                n += 1

            # save event if rnext_call field changed
            if event.rnext_call != dt:
                events = Pool().get('recurrence.event').__table__()
                cursor = Transaction().connection.cursor()

                cursor.execute(*events.update(columns=[events.rnext_call], values=[dt], where=(events.id == event.id)))
        return

    @classmethod
    def validate(cls, dates):
        super(RecurrenceDate, cls).validate(dates)
        for date in dates:
            date.check_unique_trigger()

    def check_unique_trigger(self):
        if self.event and self.trigger:
            triggers = sum(1 for d in self.event.dates if d.trigger)
            if triggers > 1:
                self.raise_user_error("Can only be one trigger")

    @classmethod
    def create(cls, vlist):
        recurdates = super(RecurrenceDate, cls).create(vlist)

        for recurdate in recurdates:
            if recurdate.trigger:
                # Set next_call after now
                recurdate.update_event_rnext_call()
        return recurdates

    @classmethod
    def write(cls, recurdates, values, *args):

        super(RecurrenceDate, cls).write(recurdates, values, *args)

        actions = iter((recurdates, values) + args)
        for records, values in zip(actions, actions):
            for r in records:
                if r.trigger:
                    # TODO: why is called two times when writing one record?
                    r.update_event_rnext_call()


class RecurrenceEvent(DeactivableMixin, ModelSQL, ModelView):
    'Scheduled Actions'
    __name__ = 'recurrence.event'

    name = fields.Char('Name', required=True)
    description = fields.Text('Description')
    recurrence = fields.Many2One('recurrence', 'Recurrence', required=True, select=True, ondelete='CASCADE')
    dates = fields.One2Many('recurrence.date', 'event', 'Dates')
    user = fields.Many2One(
        'res.user',
        'Execution User',
        required=True,
        domain=[('active', '=', False)],
        help="The user used to execute this action",
    )
    request_user = fields.Many2One(
        'res.user', 'Request User', required=True, help="The user who will receive requests in case of failure"
    )
    number_calls = fields.Integer(
        'Number of Calls',
        select=1,
        required=True,
        help=(
            'Number of times the function is called, a negative '
            'number indicates that the function will always be '
            'called'
        ),
    )
    repeat_missed = fields.Boolean('Repeat Missed')
    model = fields.Char('Model')
    function = fields.Char('Function')
    args = fields.Text('Arguments')

    rnext_call = fields.DateTime('Recurrence Next Call', states={'invisible': True}, depends=['recurrence'])
    next_call = fields.Function(fields.DateTime('Next Call'), getter='get_next_call')
    trigger_run = fields.Function(fields.Boolean('Run Trigger'), getter='get_trigger_run')

    @classmethod
    def __setup__(cls):
        super(RecurrenceEvent, cls).__setup__()
        cls._error_messages.update(
            {
                'request_title': 'Scheduled action failed',
                'request_body': (
                    "The following action failed to execute " "properly: \"%s\"\n%s\n Traceback: \n\n%s\n"
                ),
            }
        )
        cls._buttons.update({'run_once': {'icon': 'tryton-launch'}})

    @staticmethod
    def default_number_calls():
        return -1

    @staticmethod
    def default_repeat_missed():
        return True

    @staticmethod
    def default_state():
        return True

    @fields.depends('recurrence')
    def on_change_recurrence(self):
        # field rnext_call must be included in view
        if self.recurrence:
            self.rnext_call = self.recurrence.next_call
        else:
            self.rnext_call = None

    @classmethod
    def get_next_call(cls, events, name):
        return dict(
            [(event.id, next((d.date for d in event.dates if (event.active and d.trigger)), None)) for event in events]
        )

    @classmethod
    def get_trigger_run(cls, events, name):
        now = datetime.datetime.now()
        return dict(
            [
                (e.id, bool(e.recurrence.active and e.number_calls and e.next_call and e.next_call <= now))
                for e in events
            ]
        )

    @classmethod
    def send_error_message(cls, cron):
        tb_s = ''.join(traceback.format_exception(*sys.exc_info()))
        # On Python3, the traceback is already a unicode
        if hasattr(tb_s, 'decode'):
            tb_s = tb_s.decode('utf-8', 'ignore')
        subject = cls.raise_user_error('request_title', raise_exception=False)
        body = cls.raise_user_error('request_body', (cron.name, cron.__url__, tb_s), raise_exception=False)

        from_addr = config.get('email', 'from')
        to_addr = cron.request_user.email

        msg = MIMEText(body, _charset='utf-8')
        msg['To'] = to_addr
        msg['From'] = from_addr
        msg['Subject'] = Header(subject, 'utf-8')
        if not to_addr:
            logger.error(msg.as_string())
        else:
            sendmail(from_addr, to_addr, msg)

    @dualmethod
    @ModelView.button
    def run_once(cls, events):
        pool = Pool()

        for event in events:
            kwargs = {}
            if event.args:
                kwargs['args'] = literal_eval(event.args)

            if event.dates:
                kwargs['dates'] = [(d.name, bool(d.trigger), d.get_date('date')) for d in event.dates]

            Model = pool.get(event.model)
            with Transaction().set_user(event.user.id):
                getattr(Model, event.function)(**kwargs)

    @classmethod
    def run(cls, events, trigger):
        now = datetime.datetime.now()

        for event in events:
            try:
                recurrence = event.recurrence
                next_call = event.next_call
                number_calls = event.number_calls
                first = True
                trigger_date = next((d for d in event.dates if d.trigger), None)

                dt, n = recurrence.dtstart, 1
                while dt <= event.rnext_call:
                    dt = recurrence.dtstart + recurrence.get_delta(n=n)
                    n += 1

                while next_call < now and number_calls != 0:
                    event.rnext_call = dt

                    if first or event.repeat_missed:
                        try:
                            cls.run_once([event])
                        except Exception:
                            cls.send_error_message(event)

                    next_call = trigger_date.get_date('date', dt=dt)
                    dt = recurrence.dtstart + recurrence.get_delta(n=n)
                    n += 1

                    if number_calls > 0:
                        number_calls -= 1
                    first = False

                event.number_calls = number_calls
                if not number_calls:
                    event.active = False
                event.save()
            except Exception:
                logger.error('Running recurrence event %s: %s', event.id, event.name, exc_info=True)
