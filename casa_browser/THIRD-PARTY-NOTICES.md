# Third-party notices

Casa Browser's image installs the Python packages named in `requirements.txt`. This
file lists the ones whose licence asks that the licence and the source be
pointed out to whoever receives the software.

## pykeepass

- **Version:** 4.2.0
- **Licence:** GNU General Public License v3 (GPL-3.0)
- **Used for:** reading and writing the KeePass file of the built-in secrets
  store.
- **Source:** the source distribution published at
  <https://pypi.org/project/pykeepass/4.2.0/>; upstream repository
  <https://github.com/libkeepass/pykeepass>.

Casa Browser uses the package unmodified, as `pip` installs it from PyPI while the
image is built. The full licence text ships inside the installed package
(`pykeepass-4.2.0.dist-info/licenses/LICENSE`).
