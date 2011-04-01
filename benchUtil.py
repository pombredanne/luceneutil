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

import math
import types
import re
import time
import os
import shutil
import sys
import cPickle
import datetime
import constants
import common
import random
import QPSChart

# TODO
#   - allow 'onlyCat' option

# Skip the first N runs of a given category (cold) or particular task (hot):
WARM_SKIP = 10

# Skip this pctg of the slowest runs
SLOW_SKIP_PCT = 25

LOG_SUB_DIR = 'logs'

# for multi-segment index:
SEGS_PER_LEVEL = 5

DO_MIN = False

osName = common.osName

def htmlColor(v):
  if v < 0:
    return '<font color="red">%d%%</font>' % (-v)
  else:
    return '<font color="green">%d%%</font>' % v

def jiraColor(v):
  if v < 0:
    return '{color:red}%d%%{color}' % (-v)
  else:
    return '{color:green}%d%%{color}' % v

# NOTE: only detects back to 3.0
def getLuceneVersion(checkout):
  checkoutPath = checkoutToPath(checkout)
  if os.path.isdir('%s/contrib/benchmark' % checkoutPath):
    return '3.0'
  elif os.path.isdir('%s/lucene/contrib/benchmark' % checkoutPath):
    return '3.x'
  elif os.path.isdir('%s/modules/benchmark' % checkoutPath):
    return '4.0'
  else:
    raise RuntimeError('cannot determine Lucene version for checkout %s' % checkoutPath)

def checkoutToPath(checkout):
  return '%s/%s' % (constants.BASE_DIR, checkout)

def checkoutToBenchPath(checkout):
  p = checkoutToPath(checkout)
  for subDir in ('contrib/benchmark', # pre-3.1
                 'lucene/contrib/benchmark', # 3.1
                 'modules/benchmark', # 4.0
                 ):
    fullPath = '%s/%s' % (p, subDir)
    if os.path.exists(fullPath):
      return fullPath
  raise RuntimeError('could not locate benchmark under %s' % p)
    
def nameToIndexPath(name):
  return '%s/%s/index' % (constants.INDEX_DIR_BASE, name)

DEBUG = True

class SearchTask:

  def verifySame(self, other):
    if not isinstance(other, SearchTask):
      self.fail('not a SearchTask (%s)' % other)
    if self.query != other.query:
      self.fail('wrong query: %s vs %s' % (self.query, other.query))
    if self.sort != other.sort:
      self.fail('wrong sort: %s vs %s' % (self.sort, other.sort))
    # nocommit why!?
    if False and self.expandedTermCount != other.expandedTermCount:
      self.fail('wrong expandedTermCount: %s vs %s' % (self.expandedTermCount, other.expandedTermCount))
    if self.hitCount != other.hitCount:
      self.fail('wrong hitCount: %s vs %s' % (self.hitCount, other.hitCount))
    if len(self.hits) != len(other.hits):
      self.fail('wrong top hit count: %s vs %s' % (len(self.hits), len(other.hits)))

    # Collapse equals... this is sorta messy, but necessary because we
    # do not dedup by true id in SearchPerfTest
    hitsSelf = collapseDups(self.hits)
    hitsOther = collapseDups(other.hits)

    if len(hitsSelf) != len(hitsOther):
      self.fail('wrong collapsed hit count: %s vs %s' % (len(hitsSelf), len(hitsOther)))

    for i in xrange(len(hitsSelf)):
      if hitsSelf[i][1] != hitsOther[i][1]:
        self.fail('hit %s has wrong field/score value %s vs %s' % (i, hitsSelf[i][1], hitsOther[i][1]))
      if hitsSelf[i][0] != hitsOther[i][0] and i < len(hitsSelf)-1:
        self.fail('hit %s has wrong id/s %s vs %s' % (i, hitsSelf[i][0], hitsOther[i][0]))

  def fail(self, message):
    s = 'query=%s' % self.query
    if self.sort is not None:
      s += ' sort=%s' % self.sort
    raise RuntimeError('%s: %s' % (s, message))

  def __str__(self):
    s = self.query
    if self.sort is not None:
      s += ' [sort=%s]' % self.sort
    return s

  def __eq__(self, other):
    if not isinstance(other, SearchTask):
      return False
    else:
      return self.query == other.query and self.sort == other.sort

  def __hash__(self):
    return hash(self.query) + hash(self.sort)
      
  
class RespellTask:
  cat = 'Respell'

  def verifySame(self, other):
    if not isinstance(other, RespellTask):
      fail('not a RespellTask')
    if self.term != other.term:
      fail('wrong term: %s vs %s' % (self.term, other.term))
    if self.hits != other.hits:
      fail('wrong hits: %s vs %s' % (self.hits, other.hits))

  def fail(self, message):
    raise RuntimeError('respell: term=%s' % (self.term, message))

  def __str__(self):
    return 'Respell %s' % self.term

  def __eq__(self, other):
    if not isinstance(other, RespellTask):
      return False
    else:
      return self.term == other.term

  def __hash__(self):
    return hash(self.term)

    
class PKLookupTask:
  cat = 'PKLookup'

  def verifySame(self, other):
    # already "verified" in search perf test, ie, that the docID
    # returned in fact has the id that was asked for
    pass

  def __str__(self):
    return 'PK%s' % self.pkOrd

  def __eq__(self, other):
    if not isinstance(other, PKLookupTask):
      return False
    else:
      return self.pkOrd == other.pkOrd

  def __hash__(self):
    return hash(self.pkOrd)

def collapseDups(hits):
  newHits = []
  for id, v in hits:
    if len(newHits) == 0 or v != newHits[-1][1]:
      newHits.append(([id], v))
    else:
      newHits[-1][0].append(id)
      newHits[-1][0].sort()
  return newHits

reSearchTask = re.compile('cat=(.*?) q=(.*?) s=(.*?) hits=(.*?)$')
reSearchHitScore = re.compile('doc=(.*?) score=(.*?)$')
reSearchHitField = re.compile('doc=(.*?) field=(.*?)$')
reRespellHit = re.compile('(.*?) freq=(.*?) score=(.*?)$')
rePKOrd = re.compile(r'PK(.*?)\[')

def parseResults(resultsFiles):
  taskIters = []
  for resultsFile in resultsFiles:
    tasks = []
    f = open(resultsFile, 'rb')
    while True:
      line = f.readline()
      if line == '':
        break
      line = line.strip()

      if line.startswith('TASK: cat='):
        task = SearchTask()
        task.msec = float(f.readline().strip().split()[0])
        task.threadID = int(f.readline().strip().split()[1])

        m = reSearchTask.match(line[6:])
        cat, task.query, sort, hitCount = m.groups()
        task.cat = cat

        task.hitCount = int(hitCount)
        if sort == '<string: "title">':
          task.sort = 'Title'
        elif sort.startswith('<long: "datenum">'):
          task.sort = 'DateTime'
        else:
          task.sort = None

        task.hits = []
        task.expandedTermCount = 0

        while True:
          line = f.readline().strip()
          if line == '':
            break
          if line.find('expanded terms') != -1:
            task.expandedTermCount = int(line.split()[0])
            continue

          if sort == 'null':
            m = reSearchHitScore.match(line)
            id = int(m.group(1))
            score = m.group(2)
            # score stays a string so we can do "precise" ==
            task.hits.append((id, score))
          else:
            m = reSearchHitField.match(line)
            id = int(m.group(1))
            field = m.group(2)
            task.hits.append((id, field))

      elif line.startswith('TASK: respell'):
        task = RespellTask()
        task.msec = float(f.readline().strip().split()[0])
        task.threadID = int(f.readline().strip().split()[1])
        task.term = line[14:]

        task.hits = []
        while True:
          line = f.readline().strip()
          if line == '':
            break
          m = reRespellHit.search(line)
          suggest, freq, score = m.groups()
          task.hits.append((suggest, int(freq), float(score)))

      elif line.startswith('TASK: PK'):
        task = PKLookupTask()
        task.pkOrd = rePKOrd.search(line).group(1)
        task.msec = float(f.readline().strip().split()[0])
        task.threadID = int(f.readline().strip().split()[1])
      else:
        task = None
        if line.find('\tat') != -1:
          raise RuntimeError('log has exceptions')

      if task is not None:
        tasks.append(task)
    taskIters.append(tasks)
  return taskIters

def collateResults(resultIters):
  iters = []
  for results in resultIters:
    # Keyed first by category (Fuzzy1, Respell, ...) and 2nd be exact
    # task mapping to list of tasks.  For a cold run (no task repeats)
    # the 2nd map will always map to a length-1 list.
    byCat = {}
    iters.append(byCat)
    for task in results:
      if isinstance(task, SearchTask):
        key = task.cat, task.sort
      else:
        key = task.cat
      if key not in byCat:
        byCat[key] = ([], {})
      l, d = byCat[key]
      l.append(task)
      if task not in d:
        d[task] = [task]
      else:
        d[task].append(task)

  return iters

def agg(iters, cat):

  bestAvgMS = None
  lastHitCount = None

  accumMS = []
  
  # Iterate over each JVM instance we ran
  for tasksByCat in iters:

    # Maps cat -> actual tasks ran
    tasks = tasksByCat[cat]
    if len(tasks[0]) <= WARM_SKIP:
      raise RuntimeError('only %s tasks in cat %s' % (len(tasks[0]), cat))

    VERBOSE = False
    sumMS = 0.0
    totHitCount = 0
    count = 0
    sumMS = 0.0
    if VERBOSE:
      print 'AGG: cat=%s' % str(cat)

    # Iterate over each task's runs, eg, a single query will have been
    # run 40 times:
    for task, results in tasks[1].items():

      allMS = [result.msec for result in results]
      if VERBOSE:
        print '  %s' % task
        print '    before prune:'
        for t in allMS:
          print '      %.4f' % t

      # Skip warmup runs
      allMS = allMS[WARM_SKIP:]

      allMS.sort()
      minMS = allMS[0]

      # Skip slowest SLOW_SKIP_PCT runs:
      skipSlowest = int(len(allMS)*SLOW_SKIP_PCT/100.)
      if VERBOSE:
        print 'skipSlowest %s' % skipSlowest
      
      pruned = allMS[:-skipSlowest]

      if VERBOSE:
        print '    after prune:'
        for t in pruned:
          print '      %.4f' % t
          
      if DO_MIN:
        sumMS += minMS
        count += 1
      else:
        sumMS += sum(pruned)
        count += len(pruned)
      if isinstance(task, SearchTask):
        totHitCount += task.hitCount

    # AvgMS per query in category, eg if we ran 5 different queries in
    # each cat, then this is AvgMS for query in that cat:
    avgMS = sumMS/count
    accumMS.append(avgMS)
    
    if lastHitCount is None:
      lastHitCount = totHitCount
    elif totHitCount != lastHitCount:
      raise RuntimeError('different hit counts: %s vs %s' % (lastHitCount, totHitCount))

  return accumMS, totHitCount


def stats(l):
  sum = 0
  sumSQ = 0
  for v in l:
    sum += v
    sumSQ += v*v

  # min, mean, stddev
  return min(l), max(l), sum/len(l), math.sqrt(len(l)*sumSQ - sum*sum)/len(l)
  

def run(cmd, logFile=None, indent='    '):
  print '%s[RUN: %s]' % (indent, cmd)
  if logFile is not None:
    cmd = '%s > "%s" 2>&1' % (cmd, logFile)
  if os.system(cmd):
    if logFile is not None and os.path.getsize(logFile) < 5*1024:
      print open(logFile).read()
    raise RuntimeError('failed: %s [wd %s]; see logFile %s' % (cmd, os.getcwd(), logFile))

class Index:

  def __init__(self, checkout, dataSource, analyzer, codec, numDocs, numThreads, lineDocSource, doOptimize=False, dirImpl='NIOFSDirectory', doDeletions=False, ramBufferMB=-1):
    self.checkout = checkout
    self.analyzer = analyzer
    self.dataSource = dataSource
    self.codec = codec
    self.numDocs = numDocs
    self.numThreads = numThreads
    self.lineDocSource = lineDocSource
    self.doOptimize = doOptimize
    self.dirImpl = dirImpl
    self.doDeletions = doDeletions
    self.ramBufferMB = ramBufferMB 
    self.verbose = 'yes'
    self.printDPS = 'yes'
    mergeFactor = 10
    if SEGS_PER_LEVEL >= mergeFactor:
      raise RuntimeError('SEGS_PER_LEVEL (%s) is greater than mergeFactor (%s)' % (SEGS_PER_LEVEL, mergeFactor))

  def setVerbose(self, verbose):
    if verbose:
      self.verbose = 'yes'
    else:
      self.verbose = 'no'
  def setPrintDPS(self, dps):
    if dps:
      self.printDPS = 'yes'
    else: 
      self.printDPS = 'no'

  def getName(self):
    if self.doOptimize:
      s = 'opt.'
    else:
      s = ''
    return '%s.%s.%s.%snd%gM' % (self.dataSource, self.checkout, self.codec, s, self.numDocs/1000000.0)

class RunAlgs:

  def __init__(self, javaCommand):
    self.logCounter = 0
    self.results = []
    self.compiled = set()
    self.javaCommand = javaCommand
    print
    print 'JAVA:\n%s' % os.popen('java -version 2>&1').read()
    
    print
    if osName not in ('windows', 'cygwin'):
      print 'OS:\n%s' % os.popen('uname -a 2>&1').read()
    else:
      print 'OS:\n%s' % sys.platform
    
  def printEnv(self):
    print
    print 'JAVA:\n%s' % os.popen('%s -version 2>&1' % self.javaCommand).read()

    print
    if osName not in ('windows', 'cygwin'):
      print 'OS:\n%s' % os.popen('uname -a 2>&1').read()
    else:
      print 'OS:\n%s' % sys.platform

  def makeIndex(self, id, index):

    fullIndexPath = nameToIndexPath(index.getName())
    if os.path.exists(fullIndexPath):
      print '  %s: already exists' % fullIndexPath
      return fullIndexPath
    else:
      print '  %s: now create' % fullIndexPath
    if index.ramBufferMB == -1: 
      maxBufferedDocs = index.numDocs / (SEGS_PER_LEVEL*111)
    else:
      maxBufferedDocs = -1
    # maxBufferedDocs = 10000000
    
    try:
      if index.doOptimize:
        opt = 'yes'
      else:
        opt = 'no'

      if index.doDeletions:
        doDel = 'yes'
      else:
        doDel = 'no'
      
      cmd = '%s -classpath "%s" perf.Indexer %s "%s" %s %s %s %s %s %s %s %s %s %s %s' % \
            (self.javaCommand,
             self.classPathToString(self.getClassPath(index.checkout)),
             index.dirImpl,
             fullIndexPath,
             index.analyzer,
             index.lineDocSource,
             index.numDocs,
             index.numThreads,
             opt,
             index.verbose,
             index.ramBufferMB,
             maxBufferedDocs,
             index.codec,
             doDel,
             index.printDPS)
      logDir = '%s/%s' % (checkoutToBenchPath(index.checkout), LOG_SUB_DIR)
      if not os.path.exists(logDir):
        os.makedirs(logDir)
      fullLogFile = '%s/%s.%s.log' % (logDir, id, index.getName())
      print '    log %s' % fullLogFile

      t0 = time.time()
      run(cmd, fullLogFile)
      t1 = time.time()

    except:
      if os.path.exists(fullIndexPath):
        shutil.rmtree(fullIndexPath)
      raise

    return fullIndexPath, fullLogFile

  def getClassPath(self, checkout):
    path = checkoutToPath(checkout)
    cp = []
    version = getLuceneVersion(checkout)
    if version == '3.0':
      buildPath = '%s/build/classes' % path
    else:
      buildPath = '%s/lucene/build/classes' % path
    cp.append('%s/java' % buildPath)
    cp.append('%s/test' % buildPath)
    cp.append('%s/test-framework' % buildPath)

    if version == '4.0':
      cp.append('%s/modules/analysis/build/common/classes/java' % path)
      cp.append('%s/modules/analysis/build/icu/classes/java' % path)
    elif version == '3.x':
      cp.append('%s/lucene/build/contrib/analyzers/common/classes/java' % path)
    else:
      cp.append('%s/build/contrib/analyzers/common/classes/java' % path)
    cp.append('%s/lucene/build/contrib/spellchecker/classes/java' % path)

    # need benchmark path so perf.SearchPerfTest is found:
    cp.append(checkoutToBenchPath(checkout))

    return tuple(cp)

  def compile(self,competitor):
    path = checkoutToBenchPath(competitor.checkout)
    cwd = os.getcwd()
    print '  %s' % checkoutToPath(competitor.checkout)
    os.chdir(checkoutToPath(competitor.checkout))
    try:
      run('ant compile', 'compile.log')
      print '  %s' % path
      os.chdir(path)
      run('ant compile', 'compile.log')
      if path.endswith('/'):
        path = path[:-1]

      cp = self.classPathToString(self.getClassPath(competitor.checkout))
      if not os.path.exists('perf'):
        version = getLuceneVersion(competitor.checkout)
        if version == '3.0':
          subdir = '/30'
        elif version == '3.1':
          subdir = '/31'
        else:
          subdir = ''
        srcDir = '%s/perf%s' % (constants.BENCH_BASE_DIR, subdir)
        # TODO: change to just compile the code & run directly from util
        if osName in ('windows', 'cygwin'):
          run('cp -r %s perf' % srcDir)
        else:
          run('ln -s %s perf' % srcDir)
      competitor.compile(cp)
    finally:
      os.chdir(cwd)
      
  def classPathToString(self, cp):
    return common.pathsep().join(cp)

  def runSimpleSearchBench(self, id, c, repeatCount, threadCount, numTasks, coldRun, randomSeed, jvmCount, filter=None):

    if coldRun:
      # flush OS buffer cache
      print 'Drop buffer caches...'
      if osName == 'linux':
        run("sudo %s/dropCaches.sh" % constants.BENCH_BASE_DIR)
      elif osName in ('windows', 'cygwin'):
        # NOTE: requires you have admin priv
        run('%s/dropCaches.bat' % constants.BENCH_BASE_DIR)
      elif osName == 'osx':
        # NOTE: requires you install OSX CHUD developer package
        run('/usr/bin/purge')
      else:
        raise RuntimeError('do not know how to purge buffer cache on this OS (%s)' % osName)

    # randomSeed = random.Random(staticRandomSeed).randint(-1000000, 1000000)
    #randomSeed = random.randint(-1000000, 1000000)
    benchDir = checkoutToBenchPath(c.checkout)
    cwd = os.getcwd()
    os.chdir(benchDir)
    try:
      cp = self.classPathToString(self.getClassPath(c.checkout))
      logFile = '%s/%s.%s.x' % (benchDir, id, c.name)
      if os.path.exists(logFile):
        os.remove(logFile)
      if c.doSort:
        doSort = 'yes'
      else:
        doSort = 'no'

      logFiles = []
      rand = random.Random(randomSeed)
      staticSeed = rand.randint(-10000000, 1000000)
      for iter in xrange(jvmCount):
        print '    iter %s of %s' % (1+iter, jvmCount)
        randomSeed2 = rand.randint(-10000000, 1000000)      
        command = '%s -classpath "%s" perf.SearchPerfTest %s "%s" %s "%s" %s %s body %s %s %s %s %s' % \
            (self.javaCommand, cp, c.dirImpl, nameToIndexPath(c.index.getName()), c.analyzer, c.tasksFile, threadCount, repeatCount, numTasks, doSort, staticSeed, randomSeed2, c.commitPoint)
        if filter is not None:
          command += ' %s %.2f' % filter
        iterLogFile = '%s.%s' % (logFile, iter)
        print '      log: %s' % iterLogFile
        t0 = time.time()
        run(command, iterLogFile, indent='      ')
        print '      %.1f s' % (time.time()-t0)
        logFiles.append(iterLogFile)
    finally:
      os.chdir(cwd)
    return logFiles

  def getSearchLogFiles(self, id, c, jvmCount):
    logFiles = []
    benchDir = checkoutToBenchPath(c.checkout)
    logFile = '%s/%s.%s.x' % (benchDir, id, c.name)
    for iter in xrange(jvmCount):
      iterLogFile = '%s.%s' % (logFile, iter) 
      logFiles.append(iterLogFile)
    return logFiles

  def simpleReport(self, baseLogFiles, cmpLogFiles, jira=False, html=False, baseDesc='Standard', cmpDesc=None, writer=sys.stdout.write):

    baseRawResults = parseResults(baseLogFiles)
    cmpRawResults = parseResults(cmpLogFiles)

    # make sure they got identical results
    compareHits(baseRawResults, cmpRawResults)

    baseResults = collateResults(baseRawResults)
    cmpResults = collateResults(cmpRawResults)

    cats = baseResults[0].keys()
    cats.sort()

    lines = []

    warnings = []

    lines = []

    chartData = []

    resultsByCatCmp = {}

    for cat in cats:

      if type(cat) is types.TupleType:
        if cat[1] is not None:
          desc = '%s (sort %s)' % (cat[0], cat[1])
        else:
          desc = cat[0]
      else:
        desc = cat

      l0 = []
      w = l0.append
      if jira:
        w('|%s' % desc)
      elif html:
        w('<tr>')
        w('<td>%s</td>' % htmlEscape(desc))
      else:
        w('%20s' % desc)

      baseMS, baseTotHitCount = agg(baseResults, cat)
      cmpMS, cmpTotHitCount = agg(cmpResults, cat)

      baseQPS = [1000.0/x for x in baseMS]
      cmpQPS = [1000.0/x for x in cmpMS]

      minQPSBase, maxQPSBase, avgQPSBase, qpsStdDevBase = stats(baseQPS)
      minQPSCmp, maxQPSCmp, avgQPSCmp, qpsStdDevCmp = stats(cmpQPS)

      resultsByCatCmp[desc] = (minQPSCmp, maxQPSCmp, avgQPSCmp, qpsStdDevCmp)

      if DO_MIN:
        qpsBase = minQPSBase
        qpsCmp = minQPSCmp
      else:
        qpsBase = avgQPSBase
        qpsCmp = avgQPSCmp

      # print '%s: %s' % (desc, abs(qpsBase-qpsCmp) / ((maxQPSBase-minQPSBase)+(maxQPSCmp-minQPSCmp)))
      # TODO: need a real significance test here
      significant = (abs(qpsBase-qpsCmp) / (2*qpsStdDevBase+2*qpsStdDevCmp)) > 0.30

      if baseTotHitCount != cmpTotHitCount:
        warnings.append('cat=%s: hit counts differ: %s vs %s' % (desc, baseTotHitCount, cmpTotHitCount))

      if jira:
        w('|%.2f|%.2f|%.2f|%.2f' %
          (qpsBase, qpsStdDevBase, qpsCmp, qpsStdDevCmp))
      elif html:
        w('<td>%.2f</td><td>%.2f</td><td>%.2f</td><td>%.2f</td>' %
          (qpsBase, qpsStdDevBase, qpsCmp, qpsStdDevCmp))
      else:
        w('%12.2f%12.2f%12.2f%12.2f'%
          (qpsBase, qpsStdDevBase, qpsCmp, qpsStdDevCmp))

      psAvg = 100.0*(qpsCmp - qpsBase)/qpsBase

      qpsBaseBest = qpsBase + qpsStdDevBase
      qpsBaseWorst = qpsBase - qpsStdDevBase

      qpsCmpBest = qpsCmp + qpsStdDevCmp
      qpsCmpWorst = qpsCmp - qpsStdDevCmp

      psBest = int(100.0 * (qpsCmpBest - qpsBaseWorst)/qpsBaseWorst)
      psWorst = int(100.0 * (qpsCmpWorst - qpsBaseBest)/qpsBaseBest)

      if jira:
        w('|%s-%s' % (jiraColor(psWorst), jiraColor(psBest)))
      elif html:
        w('<td>%s-%s</td>' % (htmlColor(psWorst), htmlColor(psBest)))
      else:
        w('%14s' % ('%4d%% - %4d%%' % (psWorst, psBest)))

      if jira:
        w('|\n')
      else:
        w('\n')

      if constants.SORT_REPORT_BY == 'pctchange':
        sortBy = psAvg
      elif constants.SORT_REPORT_BY == 'query':
        sortBy = qs
      else:
        raise RuntimeError('invalid result sort %s' % constant.SORT_REPORT_BY)

      lines.append((sortBy, ''.join(l0)))
      if True or significant:
        chartData.append((sortBy,
                          desc,
                          qpsBase-qpsStdDevBase,
                          qpsBase+qpsStdDevBase,
                          qpsCmp-qpsStdDevCmp,
                          qpsCmp+qpsStdDevCmp,
                          ))
      
    lines.sort()
    chartData.sort()
    chartData = [x[1:] for x in chartData]

    if QPSChart.supported:
      QPSChart.QPSChart(chartData, 'out.png')
      print 'Chart saved to out.png... (wd: %s)' % os.getcwd()
                        
    w = writer

    if jira:
      w('||Task||QPS %s||StdDev %s||QPS %s||StdDev %s||Pct diff||' %
        (baseDesc, baseDesc, cmpDesc, cmpDesc))
    elif html:
      w('<table>')
      w('<tr>')
      w('<th>Task</th>')
      w('<th>QPS %s</th>' % baseDesc)
      w('<th>StdDev %s</th>' % baseDesc)
      w('<th>QPS %s</th>' % cmpDesc)
      w('<th>StdDev %s</th>' % cmpDesc)
      w('<th>%% change</th>')
      w('</tr>')
    else:
      w('%20s' % 'Task')
      w('%12s' % ('QPS %s' % baseDesc))
      w('%12s' % ('StdDev %s' % baseDesc))
      w('%12s' % ('QPS %s' % cmpDesc))
      w('%12s' % ('StdDev %s' % cmpDesc))
      w('%14s' % 'Pct diff')

    if jira:
      w('||\n')
    else:
      w('\n')

    for ign, s in lines:
      w(s)

    if html:
      w('</table>')

    for w in warnings:
      print 'WARNING: %s' % w

    return resultsByCatCmp

  def compare(self, baseline, newList, *params):

    for new in newList:
      if new.numHits != baseline.numHits:
        raise RuntimeError('baseline found %d hits but new found %d hits' % (baseline[0], new[0]))

      warmOld = baseline.warmTime
      warmNew = new.warmTime
      qpsOld = baseline.bestQPS
      qpsNew = new.bestQPS
      pct = 100.0*(qpsNew-qpsOld)/qpsOld
      #print '  diff: %.1f%%' % pct

      pct = 100.0*(warmNew-warmOld)/warmOld
      #print '  warmdiff: %.1f%%' % pct

    self.results.append([baseline] + [newList] + list(params))

  def save(self, name):
    f = open('%s.pk' % name, 'wb')
    cPickle.dump(self.results, f)
    f.close()

reFuzzy = re.compile(r'body:(.*?)\~(.*?)$')

# converts unit~0.7 -> unit~1
def fixupFuzzy(query):
  m = reFuzzy.search(query)
  if m is not None:
    term, fuzzOrig = m.groups()
    fuzz = float(fuzzOrig)
    if fuzz < 1.0:
      editDistance = int((1.0-fuzz)*len(term))
      query = query.replace('~%s' % fuzzOrig, '~%s.0' % editDistance)
  return query
  
def compareHits(r1, r2):
  if len(r1) != len(r2):
    raise RuntimeError('task counts differ: %s vs %s' % (len(r1), len(r2)))

  for iter in xrange(len(r1)):
    x1 = r1[iter]
    x2 = r2[iter]
    if len(x1) != len(x2):
      raise RuntimeError('task counts differ: %s vs %s' % (len(x1), len(x2)))
    for idx in range(len(x1)):
      x1[idx].verifySame(x2[idx])

def htmlEscape(s):
  return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def getSegmentCount(indexPath):
  segCount = 0
  for fileName in os.listdir(indexPath):
    if fileName.endswith('.fdx'):
      segCount += 1
  return segCount

class Competitor(object):

  doSort = False

  def __init__(self, name, checkout, index, dirImpl, analyzer, commitPoint, tasksFile):
    self.name = name
    self.index = index
    self.checkout = checkout
    self.commitPoint = commitPoint
    self.dirImpl = dirImpl
    self.analyzer = analyzer
    self.tasksFile = tasksFile

  def compile(self, cp):
    run('javac -classpath "%s" perf/*.java >> compile.log 2>&1' % cp, 'compile.log')

  def setTask(self, task):
    self.searchTask = self.TASKS[task];
    return self
