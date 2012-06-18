#!/usr/bin/env python

# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import shutil
import datetime
import os
import sys
import random
import common
import constants
import re

# NOTE
#   - only works in the lucene subdir, ie this runs equivalent of "ant test-core"

ROOT = common.findRootDir(os.getcwd())

osName = common.osName

JAVA_ARGS = '-Xmx512m -Xms512m'
# print
# print 'WARNING: *** running java w/ 8 GB heap ***'
# print
# JAVA_ARGS = '-Xmx8g -Xms8g'

def getArg(argName, default, hasArg=True):
  try:
    idx = sys.argv.index(argName)
  except ValueError:
    v = default
  else:
    if hasArg:
      v = sys.argv[idx+1]
      del sys.argv[idx:idx+2]
    else:
      v = True
      del sys.argv[idx]
  return v

reRepro = re.compile('NOTE: reproduce with(.*?)$', re.MULTILINE)
reDefines = re.compile('-D(.*?)=(.*?)(?: |$)')
def printReproLines(logFileName):
  f = open(logFileName, 'rb')
  print
  while True:
    line = f.readline()
    if line == '':
      break
    m = reRepro.search(line)
    codec = None
    mult = 1
    if m is not None:
      for x in reDefines.findall(line):
        k, v = x
        if k == 'testcase':
          testCase = v
        elif k == 'testmethod':
          testCase += '.%s' % v
        elif k == 'tests.seed':
          seed = v
        elif k == 'tests.codec':
          codec = v
        elif k == 'tests.multiplier':
          mult = v
        else:
          print 'WARNING: don\'t know how to repro k/v=%s' % str(x)

      if codec is not None:
        extra = '-codec %s' % codec
      else:
        extra = ''
      if extra != '':
        extra = ' ' + extra

      if mult != 1:
        if extra == '':
          extra = '-mult %s' % mult
        else:
          extra += ' -mult %s' % mult

      s = 'REPRO: %s %s -seed %s %s'%  (constants.REPRO_COMMAND_START, testCase, seed, extra)
      if constants.REPRO_COMMAND_END != '':
        s += ' %s' % constants.REPRO_COMMAND_END
      print s

tup = os.path.split(os.getcwd())

sub = os.path.split(tup[0])[0]
sub = os.path.split(sub)[1]

if os.path.exists('/dev/shm'):
  logDirName = '/dev/shm/logs/%s' % sub
else:
  logDirName = '/tmp/logs/%s' % sub
  if osName == 'windows':
    logDirName = 'c:' + logDirName

doLog = not getArg('-nolog', False, False)
doCompile = not getArg('-noc', False, False)

print 'Logging to dir %s' % logDirName

if doLog:
  if os.path.exists(logDirName):
    shutil.rmtree(logDirName)
  os.makedirs(logDirName)

if doCompile:
  print 'Compile...'
  try:
    if os.getcwd().endswith('lucene'):
      #res = os.system('ant compile-core compile-test common.compile-test > compile.log 2>&1')
      res = os.system('ant compile-core compile-test > compile.log 2>&1')
    else:
      res = os.system('ant compile-test > compile.log 2>&1')
    if res:
      print open('compile.log', 'rb').read()
      sys.exit(1)
  finally:
    os.remove('compile.log')

onlyOnce = getArg('-once', False, False)
mult = int(getArg('-mult', 1))
postingsFormat = getArg('-pf', 'random')
codec = getArg('-codec', 'random')
dir = getArg('-dir', 'random')
verbose = getArg('-verbose', False, False)
iters = int(getArg('-iters', 1))
seed = getArg('-seed', None)
nightly = getArg('-nightly', None, False)
keepLogs = getArg('-keeplogs', False, False)
heap = getArg('-heap', None, True)
if heap is not None:
  JAVA_ARGS = JAVA_ARGS.replace('512m', heap)

if len(sys.argv) == 1:
  print '\nERROR: no test specified\n'
  sys.exit(1)

tests = []
for test in sys.argv[1:]:
  if not test.startswith('org.'):
    tup = common.locateTest(test)
    if tup is None:
      print '\nERROR: cannot find test %s\n' % test
      sys.exit(1)
    testClass, testMethod = tup
    tests.append((testClass, testMethod))

JAVA_ARGS += ' -cp "%s"' % common.pathsep().join(common.getLuceneTestClassPath(ROOT))
OLD_JUNIT = os.path.exists('lib/junit-3.8.2.jar')

TEST_TEMP_DIR = 'build/test/reruns'

upto = 0
iter = 0 
while True:
  for testClass, testMethod in tests:
    print
    if testMethod is not None:
      s = '%s#%s' % (testClass, testMethod)
    else:
      s = testClass

    if doLog:
      print 'iter %s %s TEST: %s -> %s/%d.log' % (iter, datetime.datetime.now(), s, logDirName, upto)
    else:
      print 'iter %s %s TEST: %s' % (iter, datetime.datetime.now(), s)
    iter += 1
      
    command = 'java %s -DtempDir=%s -ea' % (JAVA_ARGS, TEST_TEMP_DIR)
    if False and constants.JRE_SUPPORTS_SERVER_MODE and random.randint(0, 1) == 1:
      command += ' -server'
    if False and random.randint(0, 1) == 1 and not onlyOnce:
      command += ' -Xbatch'
    #command += ' -Dtests.locale=random'
    #command += ' -Dtests.timezone=random'
    #command += ' -Dtests.lockdir=build'
    command += ' -Dtests.verbose=%s' % str(verbose).lower()
    command += ' -Dtests.infostream=%s' % str(verbose).lower()
    command += ' -Dtests.multiplier=%s' % mult
    command += ' -Dtests.iters=%s' % iters
    command += ' -Dtests.postingsformat=%s' % postingsFormat
    command += ' -Dtests.codec=%s' % codec
    command += ' -Dtests.directory=%s' % dir
    command += ' -Dtests.luceneMatchVersion=4.0'
    if constants.TESTS_LINE_FILE is not None:
      command += ' -Dtests.linedocsfile=%s' % constants.TESTS_LINE_FILE
    if nightly:
      command += ' -Dtests.nightly=true'
    if seed is not None:
      command += ' -Dtests.seed=%s' % seed
    if testMethod is not None:
      command += ' -Dtests.method=%s' % testMethod
      
    if OLD_JUNIT:
      command += ' junit.textui.TestRunner'
    else:
      command += ' org.junit.runner.JUnitCore'

    command += ' %s' % testClass

    if doLog:
      logFileName = '%s/%d.log' % (logDirName, upto)
      command += ' > %s 2>&1' % logFileName
      
    if os.path.exists(TEST_TEMP_DIR):
      print '  remove %s' % TEST_TEMP_DIR
      try:
        shutil.rmtree(TEST_TEMP_DIR)
      except OSError:
        pass
    print '  RUN: %s' % command
    res = os.system(command)

    if res:
      print '  FAILED'
      if doLog:
        printReproLines(logFileName)
      raise RuntimeError('hit fail')
    elif doLog:
      if not keepLogs:
        os.remove('%s/%d.log' % (logDirName, upto))
      else:
        upto += 1

  if onlyOnce:
    break
