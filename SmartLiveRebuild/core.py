#	vim:fileencoding=utf-8
# (c) 2010 Michał Górny <gentoo@mgorny.alt.pl>
# Released under the terms of the 3-clause BSD license or the GPL-2 license.

import bz2, os.path, pickle, signal, subprocess, sys, tempfile, time
import portage

try:
	from configparser import ConfigParser, NoOptionError
except ImportError: # py2
	from ConfigParser import ConfigParser, NoOptionError

from SmartLiveRebuild.output import out
from SmartLiveRebuild.vcs import NonLiveEbuild

class Config(ConfigParser):
	def __init__(self, settings = None):
		if settings is None:
			settings = portage.settings

		self._real_defaults = {
			'color': 'True',
			'config_file': '/etc/portage/smart-live-rebuild.conf',
			'erraneous_merge': 'True',
			'jobs': '1',
			'local_rev': 'False',
			'network': 'True',
			'offline': 'True',
			'pretend': 'False',
			'profile': 'smart-live-rebuild',
			'quickpkg': 'False',
			'setuid': str('userpriv' in settings.features),
			'type': '',
			'unprivileged_user': 'False'
		}

		self._current_section = 'DEFAULT'
		ConfigParser.__init__(self, self._real_defaults)

	def apply_optparse(self, values):
		for k in self.defaults():
			try:
				v = getattr(values, k)
				if v is None:
					raise ValueError
			except (AttributeError, ValueError):
				pass
			else:
				self.set(self._current_section, k, str(v))

	def parse_configfiles(self):
		cfl = [self.get(self._current_section, 'config_file')]
		sect = self.get(self._current_section, 'profile')

		try:
			while cfl[-1] != '' and self.read(os.path.expanduser(cfl[-1])):
				# config file chaining support
				try:
					cf = self.get(sect, 'config_file')
				except NoOptionError:
					break
				else:
					if cf not in cfl:
						cfl.append(cfl)
					else:
						break
		except Exception as e:
			out.err('Error while parsing configuration file:')
			out.err('%s: %s' % (e.__class__.__name__, e))

		if not self.has_section(sect):
			self.add_section(sect)
		self._current_section = sect

	def get_options(self):
		# values can be accessible both through keys (like a dict)
		# and through attributes (like optparse's values)
		class ConfigValues(object):
			def __getitem__(self, k):
				return getattr(self, k)

			def __setitem__(self, k, v):
				return setattr(self, k, v)

			def __str__(self):
				out = {}
				for k in dir(self):
					if not k.startswith('_'):
						out[k] = getattr(self, k)

				return str(out)

		# use the _real_defaults as type-hint
		val = ConfigValues()

		for k, v in self.items(self._current_section):
			if k not in self._real_defaults:
				val[k] = v
			elif self._real_defaults[k] in ('True', 'False'): # bool
				try:
					val[k] = self.getboolean(self._current_section, k)
				except ValueError:
					out.err('Incorrect boolean value: %s=%s' % (k, v))
					val[k] = (self._real_defaults[k] == 'True')
			elif k == 'jobs': # int
				try:
					val[k] = int(v)
				except ValueError:
					out.err('Incorrect int value: %s=%s' % (k, v))
					val[k] = int(self._real_defaults[k])
			elif k == 'type':
				if v != '':
					val[k] = v.split(',')
				else:
					val[k] = None
			else:
				val[k] = v

		return val

class SLRFailure(Exception):
	pass

def SmartLiveRebuild(opts, db = None):
	if not opts.color:
		out.monochromize()

	if opts.local_rev and not opts.network:
		out.err('The --local-rev and --no-network options can not be specified together.')
		raise SLRFailure('')
	if opts.jobs <= 0:
		out.err('The argument to --jobs option must be a positive integer.')
		raise SLRFailure('')
	elif opts.jobs > 1 and not opts.network:
		out.s1('Using parallel jobs with --no-network is inefficient, assuming no --jobs.')
		opts.jobs = 1

	childpid = None
	commpipe = None
	userok = (os.geteuid() == 0)
	if opts.setuid:
		puid = portage.data.portage_uid
		pgid = portage.data.portage_gid
		if puid and pgid:
			if not userok:
				if os.getuid() == puid:
					if not opts.pretend:
						out.s1('Running as the portage user, assuming --pretend.')
						opts.pretend = True
					if opts.quickpkg:
						out.err("Running as the portage user, --quickpkg probably won't work")
					userok = True
			elif opts.pretend and not opts.quickpkg:
				out.s1('Dropping superuser privileges ...')
				os.setuid(puid)
			else:
				out.s1('Forking to drop superuser privileges ...')
				commpipe = os.pipe()
				childpid = os.fork()
		else:
			out.err("'userpriv' is set but there's no 'portage' user in the system")

	if not opts.unprivileged_user and not userok:
		out.err('Either superuser or portage privileges are required!')
		out.out('''
This tool requires either superuser or portage (if FEATURES=userpriv is set)
privileges. If you would like to force running the update using your current
user account, please pass the --unprivileged-user option.
''')
		raise SLRFailure('')

	try:
		if not childpid:
			if childpid == 0:
				os.close(commpipe[0])
				os.setuid(puid)
			if opts.type:
				allowed = frozenset(opts.type)
			else:
				allowed = None

			out.s1('Enumerating the packages ...')

			erraneous = []
			rebuilds = {}

			vcses = {}
			envtmpf = tempfile.NamedTemporaryFile('w+b')
			try:
				if db is None:
					db = portage.db[portage.settings['ROOT']]['vartree'].dbapi

				for cpv in db.cpv_all():
					try:
						inherits = db.aux_get(cpv, ['INHERITED'])[0].split()

						for vcs in inherits:
							if vcs not in vcses:
								if allowed and vcs not in allowed:
									vcses[vcs] = None
								else:
									try:
										modname = 'SmartLiveRebuild.vcs.%s' % vcs
										vcses[vcs] = __import__(modname, globals(), locals(), ['myvcs']).myvcs
									except ImportError:
										vcses[vcs] = None

							if vcses[vcs] is not None:
								env = bz2.BZ2File('%s/environment.bz2' % db.getpath(cpv), 'r')
								vcs = vcses[vcs](cpv, env, envtmpf, opts)
								env.close()
								if opts.network or vcs.getsavedrev():
									dir = vcs.getpath()
									if dir not in rebuilds:
										rebuilds[dir] = vcs
									else:
										rebuilds[dir].append(vcs)
					except KeyboardInterrupt:
						raise
					except NonLiveEbuild as e:
						out.err('%s: %s' % (cpv, e))
					except Exception as e:
						out.err('Error enumerating %s: [%s] %s' % (cpv, e.__class__.__name__, e))
						erraneous.append(cpv)
			finally:
				envtmpf.close()

			if opts.jobs == 1:
				out.s1('Updating the repositories...')
			else:
				out.s1('Updating the repositories using %s%d%s parallel jobs...' % (out.white, opts.jobs, out.s1reset))
			packages = []

			processes = []
			items = list(rebuilds.items())
			while True:
				try:
					if len(processes) < opts.jobs and len(items) > 0:
						(dir, vcs) = items.pop(0)
						try:
							vcs.startupdate()
							if opts.jobs == 1:
								ret = vcs.endupdate(True)
							else:
								processes.append(vcs)
						except KeyboardInterrupt:
							vcs.abortupdate()
							raise
					elif len(processes) == 0: # which is true if jobs == 1 too
						break
					else:
						time.sleep(0.3)

					for vcs in processes:
						ret = vcs.endupdate()
						if ret is not None:
							processes.remove(vcs)
							break

					if ret:
						packages.extend(vcs.cpv)
				except KeyboardInterrupt:
					out.err('Updates interrupted, proceeding with already updated repos.')
					for vcs in processes:
						vcs.abortupdate()
					break
				except Exception as e:
					out.err('Error updating %s: [%s] %s' % (vcs.cpv, e.__class__.__name__, e))
					if opts.jobs != 1 and vcs in processes:
						processes.remove(vcs)
					erraneous.extend(vcs.cpv)

			if childpid == 0:
				pdata = {'packages': packages, 'erraneous': erraneous}
				pipe = os.fdopen(commpipe[1], 'wb')
				pickle.dump(pdata, pipe, pickle.HIGHEST_PROTOCOL)
				pipe.flush()
				pipe.close()
				os._exit(0)
		else:
			os.close(commpipe[1])
			pipe = os.fdopen(commpipe[0], 'rb')
			sigint = signal.getsignal(signal.SIGINT)
			signal.signal(signal.SIGINT, signal.SIG_IGN)
			try:
				pdata = pickle.load(pipe)
			except EOFError: # child terminated early
				raise SLRFailure('')
			signal.signal(signal.SIGINT, sigint)
			packages = pdata['packages']
			erraneous = pdata['erraneous']

		if opts.erraneous_merge and len(erraneous) > 0:
			packages.extend(erraneous)

		if opts.quickpkg and len(packages) >= 1:
			out.s1('Calling quickpkg to create %s%d%s binary packages ...' % (out.white, len(packages), out.s1reset))
			cmd = ['/usr/sbin/quickpkg', '--include-config=y']
			cmd.extend(['=%s' % x for x in packages])
			out.s2(' '.join(cmd))
			subprocess.Popen(cmd, stdout=sys.stderr).wait()

		if opts.offline:
			if opts.erraneous_merge and len(erraneous) > 0:
				out.s1('Merging update-failed packages, assuming --no-offline.')
				opts.offline = False
			else:
				os.environ['ESCM_OFFLINE'] = 'true'

		if len(packages) < 1:
			out.s1('No updates found')
		else:
			out.s1('Found %s%d%s packages to rebuild.' % (out.white, len(packages), out.s1reset))
	finally:
		if childpid: # make sure that we leave no orphans
			os.kill(childpid, signal.SIGTERM)

	return packages