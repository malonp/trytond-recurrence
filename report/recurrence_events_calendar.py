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

from dateutil import relativedelta
from trytond.report import Report

__all__ = ['EventsList']


class EventsList(Report):
    __name__ = 'recurrence.events_list'

    @classmethod
    def get_context(cls, records, data):
        report_context = super(EventsList, cls).get_context(records, data)

        ocurrences = {}

        for event in records:
            from_date = max(datetime.datetime.today() - event.recurrence.get_delta(n=2), event.recurrence.dtstart)
            to_date = datetime.date.today() + event.recurrence.get_delta(n=2) + relativedelta.relativedelta(years=1)

            for date in filter(lambda x: x.trigger, event.dates):
                rnext_call = event.recurrence.get_next_call('next_call', now=from_date)
                next_call = date.get_date('date', dt=rnext_call)
                while next_call.date() < to_date:
                    ocurrences[next_call.date()] = [
                        (d.name, d.get_date('date', dt=rnext_call).date()) for d in event.dates if not d.trigger
                    ]
                    rnext_call = event.recurrence.get_next_call(
                        'next_call', now=rnext_call + relativedelta.relativedelta(days=1)
                    )
                    next_call = date.get_date('date', dt=rnext_call)

        report_context['ocurrences'] = ocurrences
        report_context['title'] = ' '.join([event.name for event in records])

        return report_context
