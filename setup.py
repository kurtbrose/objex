"""*objex: the object structure exporter*

objex allows exporting object structure without
object data to a sqlite database that can be
transferred to another machine and debugged

this allows memory leaks to be debugged in
an offline fashion without exposing customer data
"""

from setuptools import setup


__author__ = 'Kurt Rose'
__version__ = '0.14dev'
__contact__ = 'kurt@kurtrose.com'
__url__ = 'https://github.com/kurtbrose/objex'
__license__ = 'BSD'


setup(
    name='objex',
    version=__version__,
    description='object structure exporter',
    long_description=__doc__,
    author=__author__,
    author_email=__contact__,
    url=__url__,
    packages=['objex'],
    install_requires=['boltons'],
    extras_require={},
    license=__license__,
    platforms='any',
    classifiers=[
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3.6']
)
