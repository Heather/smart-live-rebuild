#	vim:fileencoding=utf-8
# (c) 2011 Michał Górny <mgorny@gentoo.org>
# Released under the terms of the 3-clause BSD license or the GPL-2 license.

from smartliverebuild.vcs import VCSSupport

class HgSupport(VCSSupport):
	reqenv = ['EHG_REPO_URI', 'EHG_REVISION', 'HG_REV_ID']

	trustopt = ['--config', 'trusted.users=portage']

	def __str__(self):
		return self.env['EHG_REPO_URI']

	def getsavedrev(self):
		return self.env['HG_REV_ID']

	@staticmethod
	def revcmp(oldrev, newrev):
		return newrev.startswith(oldrev)

	def getupdatecmd(self):
		return 'hg identify --id --rev %s %s %s' % (
				self.env['EHG_REVISION'], self.env['EHG_REPO_URI'],
				' '.join(self.trustopt))