#!/usr/bin/env python

'''
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

from resource_management.libraries.functions import format
from resource_management.core.resources.system import File, Execute
import threading

def livy_service(name, upgrade_type=None, action=None):
  import params

  if action == 'start':
    livyserver_no_op_test = format(
      'ls {livy_server_pid_file} >/dev/null 2>&1 && ps -p `cat {livy_server_pid_file}` >/dev/null 2>&1')
    Execute(format('{livy_server_start}'),
            user=params.livy_user,
            environment={'JAVA_HOME': params.java_home},
            not_if=livyserver_no_op_test
    )

  elif action == 'stop':
    Execute(format('{livy_server_stop}'),
            user=params.livy_user,
            environment={'JAVA_HOME': params.java_home}
            )
    File(params.livy_server_pid_file,
        action="delete"
        )




