#!python
# Copyright 2013 Google Inc. All Rights Reserved.
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
"""A utility script to automate the process of symbolizing a SyzyASan
minidump.
"""

from collections import namedtuple
import optparse
import os
import re
import subprocess
import sys


_SENTINEL = 'ENDENDEND'
_DEFAULT_CDB_PATH = \
    r'c:\Program Files (x86)\Debugging Tools for Windows (x86)\cdb.exe'
_BAD_ACCESS_INFO_FRAME = "asan_rtl!agent::asan::AsanRuntime::OnError"
_GET_BAD_ACCESS_INFO_COMMAND = "dt error_info"
_GET_ALLOC_STACK_COMMAND = "dps @@(&error_info->alloc_stack) "  \
                           "l@@(error_info->alloc_stack_size);"
_GET_FREE_STACK_COMMAND = "dps @@(&error_info->free_stack) "  \
                          "l@@(error_info->free_stack_size);"
_ERROR_HELP_URL = "You can go to \
https://code.google.com/p/syzygy/wiki/SyzyASanBug to get more information \
about how to treat this bug."


ASanReport = namedtuple("ASanReport", "bad_access_info crash_stack alloc_stack "
                        "free_stack")


_STACK_FRAME_RE = re.compile("""
    ^
    (?P<args>([0-9A-F]+\ +)+)
    (?:
      (?P<module>[^ ]+)(!(?P<location>.*))? |
      (?P<address>0x[0-9a-f]+)
    )
    $
    """, re.VERBOSE | re.IGNORECASE)

_CHROME_RE = re.compile('(chrome[_0-9A-F]+)', re.VERBOSE | re.IGNORECASE)


def _Command(debugger, command):
  debugger.stdin.write(command + '; .echo %s\n' % _SENTINEL)
  lines = []
  while True:
    line = debugger.stdout.readline().rstrip()
    # TODO(sebmarchand): Check for equality to avoid to stop if a line contains
    #     the sentinel value.
    if _SENTINEL in line:
      break
    lines.append(line)
  return lines


def NormalizeChromeSymbol(symbol):
  return _CHROME_RE.sub('chrome_dll', symbol)


def NormalizeStackTrace(stack_trace):
  trace_hash = 0
  output_trace = []
  for line in stack_trace:
    m = _STACK_FRAME_RE.match(line)
    if not m:
      continue
    address = m.group('address')
    module = m.group('module')
    location = m.group('location')
    if address:
      output_trace.append(address)
    else:
      module = NormalizeChromeSymbol(module)
      if location:
        location = NormalizeChromeSymbol(location)
      else:
        location = "unknown"
      frame = '%s!%s' % (module, location)
      output_trace.append(frame)

  return output_trace


def ProcessMinidump(minidump_filename, cdb_path, pdb_path):
  debugger = subprocess.Popen([cdb_path,
                               '-z', minidump_filename],
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

  if pdb_path is not None:
    _Command(debugger, '.sympath %s' % pdb_path)
    _Command(debugger, '.reload /fi chrome.dll')
    _Command(debugger, '.reload /fi syzyasan_rtl.dll')
    _Command(debugger, '.symfix')

  # Enable the line number informations.
  _Command(debugger, '.lines')

  # Get the SyzyASan crash stack and try to find the frame containing the
  # bad access info structure.

  asan_crash_stack = _Command(debugger, 'kv')

  bad_access_info_frame = 0;
  for line in NormalizeStackTrace(asan_crash_stack):
    if line.find(_BAD_ACCESS_INFO_FRAME) == -1:
      bad_access_info_frame += 1
    else:
      break

  if bad_access_info_frame == -1:
    # End the debugging session.
    debugger.stdin.write('q\n')
    debugger.wait()
    print "Unable to find the %s frame for %d." % (_BAD_ACCESS_INFO_FRAME,
                                                   minidump_filename)
    return

  # Get the allocation, free and crash stack traces.
  _Command(debugger, '.frame %X' % bad_access_info_frame)
  bad_access_info = _Command(debugger, _GET_BAD_ACCESS_INFO_COMMAND)
  bad_access_info.pop(0)
  alloc_stack = (
      NormalizeStackTrace(_Command(debugger, _GET_ALLOC_STACK_COMMAND)))
  free_stack = NormalizeStackTrace(_Command(debugger, _GET_FREE_STACK_COMMAND))
  _Command(debugger, '.ecxr')
  crash_stack = NormalizeStackTrace(_Command(debugger, 'kv'))

  # End the debugging session.
  debugger.stdin.write('q\n')
  debugger.wait()

  report = ASanReport(bad_access_info = bad_access_info,
                      crash_stack = crash_stack,
                      alloc_stack = alloc_stack,
                      free_stack = free_stack)

  return report


def PrintASanReport(report):
  # Print the crash report.
  print 'Bad access information:'
  for line in report.bad_access_info: print line
  print '\nCrash stack:'
  for line in report.crash_stack: print line
  print '\nAllocation stack:'
  for line in report.alloc_stack: print line
  if len(report.free_stack) != 0:
    print '\nFree stack:'
    for line in report.free_stack: print line
  print '\n', _ERROR_HELP_URL

  return


_USAGE = """\
%prog [options]

Symbolizes a minidump that has been generated by SyzyASan. This prints the
crash, alloc and free stack traces and gives more information about the crash.
"""


def _ParseArguments():
  parser = optparse.OptionParser(usage=_USAGE)
  # TODO(sebmarchand): Move this to an argument instead of a switch?
  parser.add_option('--minidump',
                    help='The input minidump.')
  parser.add_option('--cdb-path',
                    default=_DEFAULT_CDB_PATH,
                    help='(Optional) The path to cdb.exe.')
  parser.add_option('--pdb-path',
                    help='(Optional) The path to the folder containing the'
                         ' PDBs.')
  (opts, args) = parser.parse_args()

  if len(args):
    parser.error('Unexpected argument(s).')

  if not opts.minidump:
    parser.error('You must provide a minidump.')

  opts.minidump = os.path.abspath(opts.minidump)

  return opts


def main():
  """Parse arguments and do the symbolization."""

  opts = _ParseArguments()

  report = ProcessMinidump(opts.minidump, opts.cdb_path, opts.pdb_path)
  PrintASanReport(report)

  return 0


if __name__ == '__main__':
  sys.exit(main())
