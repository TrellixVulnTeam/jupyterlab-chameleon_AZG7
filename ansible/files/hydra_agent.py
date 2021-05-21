#!/usr/bin/env python
# Copyright 2021 University of Chicago
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
from re import L
import shlex
import subprocess
import sys
import time

from jupyter_core.paths import jupyter_runtime_dir

parser = argparse.ArgumentParser("hydra")
parser.add_argument("--id", required=True, help="The ID assigned to the kernel")
parser.add_argument("--kernel", required=True, help="The kernel implementation to start")
parser.add_argument("--timeout", default=10, help="A timeout on kernel startup")
parser.add_argument("--launcher", default="hydra-subkernel", help="The kernel launch binary")
parser.add_argument("--log-file", dest="log_to_file", action="store_true")
parser.add_argument("--no-log-file", dest="log_to_file", action="store_false")
parser.set_defaults(log_to_file=True)
args = parser.parse_args(sys.argv[1:])

runtime_dir = jupyter_runtime_dir()
connection_file = os.path.join(runtime_dir, f"kernel-{args.id}.json")
log_file = os.path.join(runtime_dir, f"kernel-{args.id}.log")

cmd_str = f"{args.launcher} --kernel {args.kernel} --KernelManager.connection_file {connection_file}"
if args.log_to_file:
    cmd_str += f" --log-file {log_file}"
kernel = subprocess.Popen(
    shlex.split(cmd_str),
    stderr=subprocess.STDOUT,
    start_new_session=True
)

start = time.perf_counter()
while not os.path.exists(connection_file):
    if time.perf_counter() - start > args.timeout:
        ret = kernel.poll()
        if ret is not None:
            msg = f"Kernel failed with status {ret}."
        msg = "Failed to find connection file, did the kernel fail to start?"
        if args.log_to_file:
            msg += f" See {log_file} for details."
        raise TimeoutError(msg)
    time.sleep(0.1)

print(kernel.pid)
with open(connection_file, "r") as f:
    print(f.read())
