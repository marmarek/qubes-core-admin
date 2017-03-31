# -*- encoding: utf8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2017 Marek Marczykowski-Górecki
#                               <marmarek@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, see <http://www.gnu.org/licenses/>.
import argparse
import logging
import logging.handlers

import sys

import qubespolicy

parser = argparse.ArgumentParser(description="Evaluate qrexec policy")

parser.add_argument("--assume-yes-for-ask", action="store_true",
    dest="assume_yes_for_ask", default=False,
    help="Allow run of service without confirmation if policy say 'ask'")
parser.add_argument("--just-evaluate", action="store_true",
    dest="just_evaluate", default=False,
    help="Do not run the service, only evaluate policy; "
         "retcode=0 means 'allow'")
parser.add_argument('domain_id', metavar='src-domain-id',
    help='Source domain ID (Xen ID or similar, not Qubes ID)')
parser.add_argument('domain', metavar='src-domain-name',
    help='Source domain name')
parser.add_argument('target', metavar='dst-domain-name',
    help='Target domain name')
parser.add_argument('service_name', metavar='service-name',
    help='Service name')
parser.add_argument('process_ident', metavar='process-ident',
    help='Qrexec process identifier - for connecting data channel')


def main(args=None):
    args = parser.parse_args(args)

    # Add source domain information, required by qrexec-client for establishing
    # connection
    caller_ident = args.process_ident + "," + args.domain + "," + args.domain_id
    log = logging.getLogger('qubespolicy')
    log.setLevel(logging.INFO)
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    log.addHandler(handler)
    log_prefix = 'qrexec: {}: {} -> {}: '.format(
        args.service_name, args.domain, args.target)
    try:
        system_info = qubespolicy.get_system_info()
    except qubespolicy.QubesMgmtException as e:
        log.error(log_prefix + 'error getting system info: ' + str(e))
        return 1
    try:
        policy = qubespolicy.Policy(args.service_name)
        action = policy.evaluate(system_info, args.domain, args.target)
        if action.action == qubespolicy.Action.ask:
            # late import to save on time for allow/deny actions
            import qubespolicy.rpcconfirmation as rpcconfirmation
            entries_info = system_info['domains'].copy()
            for dispvm_base in system_info['domains']:
                if not system_info['domains'][dispvm_base]['dispvm_allowed']:
                    continue
                dispvm_api_name = '$dispvm:' + dispvm_base
                entries_info[dispvm_api_name] = \
                    system_info['domains'][dispvm_base].copy()
                entries_info[dispvm_api_name]['icon'] = \
                    entries_info[dispvm_api_name]['icon'].replace('app', 'disp')

            response = rpcconfirmation.confirm_rpc(
                entries_info, args.domain, args.service_name,
                action.targets_for_ask, action.target)
            if response:
                action.handle_user_response(True, response)
            else:
                action.handle_user_response(False)
        log.info(log_prefix + 'allowed')
        action.execute(caller_ident)
    except qubespolicy.PolicySyntaxError as e:
        log.error(log_prefix + 'error loading policy: ' + str(e))
        return 1
    except qubespolicy.AccessDenied as e:
        log.info(log_prefix + 'denied: ' + str(e))
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
