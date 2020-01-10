#!/usr/bin/python
#
# Copyright 2008 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#

import os

"""
This script generates the VDS installation verificator
python script and embeds the VT/SVM check utility into
it.
"""
test_vt_svm = open("test_vt_svm").read()

print """#!/usr/bin/python
#
# Copyright 2008 Red Hat, Inc. and/or its affiliates.
#
# Licensed to you under the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.  See the files README and
# LICENSE_GPL_v2 which accompany this distribution.
#
"""
print "# This is a hex representation of the test_vt_svm binary"

print "test_vt_svm = ", repr(test_vt_svm)

try:
    rev = os.environ['REVISION']
except:
    rev = 'devel'

print "\nREVISION = '%s'\n" % rev

print open("vds_compat.py").read()
