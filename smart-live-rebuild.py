#!/usr/bin/python
#	vim:fileencoding=utf-8
# Check all live ebuilds for updates and rebuild them if necessary.
# (C) 2010 Michał Górny <gentoo@mgorny.alt.pl>
# Released under the terms of the 3-clause BSD license

PV = '0.1'

import bz2, codecs, re, os, sys, subprocess
import portage

from optparse import OptionParser

root = portage.settings['ROOT']
db = portage.db[root]["vartree"].dbapi

utfdec = codecs.getdecoder('utf8')
declre = re.compile('^declare -[-x] ([A-Z_]+)="(.*)"$')

rebuilds = {}

class out:
	red = '\033[1;31m'
	green = '\033[32m'
	lime = '\033[1;32m'
	yellow = '\033[1;33m'
	cyan = '\033[36m'
	turq = '\033[1;36m'
	white = '\033[1;37m'
	reset = '\033[0m'

	s1reset = lime
	s2reset = green
	s3reset = cyan
	errreset = yellow

	@classmethod
	def monochromize(self):
		for k in dir(self):
			if not k.startswith('_'):
				v = getattr(self, k)
				if isinstance(v, str) and v.startswith('\033'):
					setattr(self, k, '')

	@classmethod
	def s1(self, msg):
		self.out('%s*** %s%s\n' % (self.s1reset, msg, self.reset))
	@classmethod
	def s2(self, msg):
		self.out('%s->%s  %s\n' % (self.s2reset, self.reset, msg))
	@classmethod
	def s3(self, msg):
		self.out('%s-->%s %s\n' % (self.s3reset, self.reset, msg))

	@classmethod
	def err(self, msg):
		self.out('%s!!!%s %s%s%s\n' % (self.red, self.reset, self.errreset, msg, self.reset))

	@staticmethod
	def out(msg):
		sys.stderr.write(msg)

class VCSSupport:
	inherit = None
	reqenv = []

	@classmethod
	def match(self, inherits):
		if self.inherit is None:
			raise NotImplementedError('VCS class needs to either override inherit or match()')
		return (self.inherit in inherits)

	def __init__(self, cpv, env):
		self.cpv = [cpv]
		self.env = {}
		# clone both
		self.reqenv = list(self.reqenv)
		self.optenv = list(self.optenv)

		if len(self.reqenv) + len(self.optenv) >= 1:
			for l in env:
				m = declre.match(l)
				if m is not None:
					k = m.group(1)
					inreq = (k in self.reqenv)
					if inreq or k in self.optenv:
						self.env[k] = m.group(2)
						if inreq:
							self.reqenv.remove(k)
						else:
							self.optenv.remove(k)
						if len(self.reqenv) + len(self.optenv) < 1:
							break
			else:
				if len(self.reqenv) >= 1:
					raise KeyError('Environment does not declare: %s' % self.reqenv)
				else:
					for k in self.optenv:
						self.env[k] = None

	def getpath(self):
		raise NotImplementedError('VCS class needs to override getpath()')

	def append(self, vcs):
		if not isinstance(vcs, self.__class__):
			raise ValueError('Unable to append %s to %s' % (vcs.__class__, self.__class__))
		self.cpv.append(vcs.cpv[0])

	def getrev(self):
		raise NotImplementedError('VCS class needs to override getrev() or update()')

	@staticmethod
	def call(cmd):
		return subprocess.Popen(cmd, stdout=subprocess.PIPE).communicate()[0].split('\n')[0]

	def getupdatecmd(self):
		raise NotImplementedError('VCS class needs to override getupdatecmd(), doupdate() or update()')

	def doupdate(self):
		cmd = self.getupdatecmd()
		out.s3(cmd)
		ret = subprocess.Popen(cmd, shell=True).wait()
		return (ret == 0)

	def update(self):
		out.s2(unicode(self))
		os.chdir(self.getpath())

		oldrev = self.getrev()
		if not self.doupdate():
			out.err('update failed')
		else:
			newrev = self.getrev()

			if oldrev == newrev:
				out.s3('at rev %s%s%s (no changes)' % (out.green, oldrev, out.reset))
				return False
			else:
				out.s3('update from %s%s%s to %s%s%s' % (out.green, oldrev, out.reset, out.lime, newrev, out.reset))
				return True

	def __unicode__(self):
		return self.cpv

class GitSupport(VCSSupport):
	inherit = 'git'
	reqenv = ['EGIT_BRANCH', 'EGIT_PROJECT', 'EGIT_STORE_DIR', 'EGIT_UPDATE_CMD']
	optenv = ['EGIT_HAS_SUBMODULES', 'EGIT_OPTIONS', 'EGIT_REPO_URI']

	def __init__(self, cpv, env):
		VCSSupport.__init__(self, cpv, env)
		if self.env['EGIT_HAS_SUBMODULES'] == 'true':
			raise NotImplementedError('Submodules are not supported')

	def getpath(self):
		return u'%s/%s' % (self.env['EGIT_STORE_DIR'], self.env['EGIT_PROJECT'])

	def __unicode__(self):
		if self.env['EGIT_REPO_URI'] is not None:
			return self.env['EGIT_REPO_URI']
		else:
			return self.cpv

	def getrev(self):
		return self.call(['git', 'rev-parse', self.env['EGIT_BRANCH']])

	def getupdatecmd(self):
		return '%s %s origin %s:%s' % (self.env['EGIT_UPDATE_CMD'], self.env['EGIT_OPTIONS'], self.env['EGIT_BRANCH'], self.env['EGIT_BRANCH'])

vcsl = [GitSupport]

def main(argv):
	opt = OptionParser(
			usage='%prog [options] -- [emerge options]',
			version='%%prog %s' % PV,
			description='Enumerate all live packages in system, check their repositories for updates and remerge the updated ones.'
	)
	opt.add_option('-C', '--no-color', action='store_false', dest='color', default=True,
		help='Disable colorful output')
	opt.add_option('-p', '--pretend', action='store_true', dest='pretend', default=False,
		help='Only print a list of the packages which were updated; do not call emerge to rebuild them.')
	opt.add_option('-R', '--record', action='store_true', dest='record', default=False,
		help='Omit passing --oneshot option to portage, and thus add updated packages to the @world set.')
	(opts, args) = opt.parse_args(argv[1:])

	if not opts.color:
		out.monochromize()

	out.s1('Enumerating packages ...')

	for cpv in db.cpv_all():
		def getenv():
			fn = u'%s/environment.bz2' % db.getpath(cpv)
			f = bz2.BZ2File(fn, 'r')
			return utfdec(f.read())[0].split('\n')

		try:
			inherits = db.aux_get(cpv, ['INHERITED'])[0].split()

			for vcs in vcsl:
				if vcs.match(inherits):
					env = getenv()
					vcs = vcs(cpv, env)
					dir = vcs.getpath()
					if dir not in rebuilds:
						rebuilds[dir] = vcs
					else:
						rebuilds[dir].append(vcs)
		except KeyboardInterrupt:
			raise
		except Exception as e:
			out.err('Error enumerating %s: [%s] %s' % (cpv, e.__class__.__name__, e))

	out.s1('Updating repositories ...')
	packages = []

	for (dir, vcs) in rebuilds.items():
		if vcs.update():
			packages.extend(vcs.cpv)

	if len(packages) < 1:
		out.s1('No updates found')
	elif opts.pretend:
		out.s1('Printing list of updated packages ...')
		for p in packages:
			print p
	else:
		out.s1('Calling emerge to rebuild %s%d%s packages ...' % (out.white, len(packages), out.s1reset))
		cmd = ['emerge']
		if not opts.record:
			cmd.append('--oneshot')
		cmd.extend(args)
		cmd.extend(['=%s' % x for x in packages])
		out.s2(' '.join(cmd))
		os.execv('/usr/bin/emerge', cmd)

	return 0

if __name__ == '__main__':
	sys.exit(main(sys.argv))
