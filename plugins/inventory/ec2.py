#!/usr/bin/env python

'''
EC2 external inventory script
=================================

Generates inventory that Ansible can understand by making API request to
AWS EC2 using the Boto library.

NOTE: This script assumes Ansible is being executed where the environment
variables needed for Boto have already been set:
    export AWS_ACCESS_KEY_ID='AK123'
    export AWS_SECRET_ACCESS_KEY='abc123'

If you're using eucalyptus you need to set the above variables and
you need to define:

    export EC2_URL=http://hostname_of_your_cc:port/services/Eucalyptus

For more details, see: http://docs.pythonboto.org/en/latest/boto_config_tut.html

When run against a specific host, this script returns the following variables:
 - ec2_ami_launch_index
 - ec2_architecture
 - ec2_association
 - ec2_attachTime
 - ec2_attachment
 - ec2_attachmentId
 - ec2_client_token
 - ec2_deleteOnTermination
 - ec2_description
 - ec2_deviceIndex
 - ec2_dns_name
 - ec2_eventsSet
 - ec2_group_name
 - ec2_hypervisor
 - ec2_id
 - ec2_image_id
 - ec2_instanceState
 - ec2_instance_type
 - ec2_ipOwnerId
 - ec2_ip_address
 - ec2_item
 - ec2_kernel
 - ec2_key_name
 - ec2_launch_time
 - ec2_monitored
 - ec2_monitoring
 - ec2_networkInterfaceId
 - ec2_ownerId
 - ec2_persistent
 - ec2_placement
 - ec2_platform
 - ec2_previous_state
 - ec2_private_dns_name
 - ec2_private_ip_address
 - ec2_publicIp
 - ec2_public_dns_name
 - ec2_ramdisk
 - ec2_reason
 - ec2_region
 - ec2_requester_id
 - ec2_root_device_name
 - ec2_root_device_type
 - ec2_security_group_ids
 - ec2_security_group_names
 - ec2_shutdown_state
 - ec2_sourceDestCheck
 - ec2_spot_instance_request_id
 - ec2_state
 - ec2_state_code
 - ec2_state_reason
 - ec2_status
 - ec2_subnet_id
 - ec2_tenancy
 - ec2_virtualization_type
 - ec2_vpc_id

These variables are pulled out of a boto.ec2.instance object. There is a lack of
consistency with variable spellings (camelCase and underscores) since this
just loops through all variables the object exposes. It is preferred to use the
ones with underscores when multiple exist.

In addition, if an instance has AWS Tags associated with it, each tag is a new
variable named:
 - ec2_tag_[Key] = [Value]

Security groups are comma-separated in 'ec2_security_group_ids' and
'ec2_security_group_names'.

Overriding convfiguration at run-time
-------------------------------------

It is possible to override ini file variables by setting environment variables
to alternative values. Name the variable as per the ini file variable, but in
upper case and prefixed INV_

Thus to override destination_variable set INV_DESTINATION_VARIABLE

This allows for making variations at run-time such as:

    $ INV_VPC_DESTINATION_VARIABLE=ip_address ansible-playbook \
      -i /path/to/ec2.py ...
'''

# (c) 2012, Peter Sankauskas
#
# This file is part of Ansible,
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Additional modifications added by Matthew Pontefract (ReThought) and
# others as visible in VCS.

######################################################################

import sys
import os
import argparse
import re
from time import time
import boto
from boto import ec2
from boto import rds
import ConfigParser

try:
    import json
except ImportError:
    import simplejson as json


class Ec2Inventory(object):
    def __init__(self):
        ''' Main execution path '''

        # Inventory grouped by instance IDs, tags, security groups, regions,
        # and availability zones
        self.inventory = {}

        # Index of hostname (address) to instance ID
        self.index = {}

        # Data for optional tag processing options
        self.tag_processors = {}
        self.tag_processors['list_value_tags'] = []
        self.tag_processors['tag_to_group'] = {}
        self.tag_processors['include_hosts_by_tag'] = {}

        # Read settings and parse CLI arguments
        self.read_settings()
        self.parse_cli_args()
        self.parse_environment()

        # Cache
        if self.args.refresh_cache:
            self.do_api_calls_update_cache()
        elif not self.is_cache_valid():
            self.do_api_calls_update_cache()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()

        elif self.args.list:
            # Display list of instances for inventory
            if len(self.inventory) == 0:
                data_to_print = self.get_inventory_from_cache()
            else:
                data_to_print = self.json_format_dict(self.inventory, True)

        print data_to_print


    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''

        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                if os.path.isfile(self.cache_path_index):
                    return True

        return False


    def read_settings(self):
        ''' Reads the settings from the ec2.ini file
        
        The ini file is looked for in the following locations, in the following
        order:

          - in the file referenced by EC2_INI_FILE environment variable
          - in the directory referenced by EC2_INI_DIR environment variable
          - in the current working directory
          - in the directory where ec2.py resides (the real file, not softlink)

        thus you can set EC2_INI_FILE=/home/foo/myec2.ini or create an 
        ec2.ini in a directory the path to which is assigned EC2_INI_DIR

        The final option ensures backwards compatibility with the original
        behaviour.
        '''
        _local_option = os.path.join(os.getcwd(), 'ec2.ini')
        if 'EC2_INI_FILE' in os.environ:
            source_file = os.environ['EC2_INI_FILE']
        elif 'EC2_INI_DIR' in os.environ:
            source_file = os.path.join(os.environ['EC2_INI_DIR'], 'ec2.ini')
        elif os.path.exists(_local_option):
            source_file = _local_option
        else:
            source_file = os.path.join(
                os.path.dirname(os.path.realpath(__file__)), 'ec2.ini')

        config = ConfigParser.SafeConfigParser()
        config.read(source_file)

        # is eucalyptus?
        self.eucalyptus_host = None
        self.eucalyptus = False
        if config.has_option('ec2', 'eucalyptus'):
            self.eucalyptus = config.getboolean('ec2', 'eucalyptus')
        if self.eucalyptus and config.has_option('ec2', 'eucalyptus_host'):
            self.eucalyptus_host = config.get('ec2', 'eucalyptus_host')

        # Regions
        self.regions = []
        configRegions = config.get('ec2', 'regions')
        configRegions_exclude = config.get('ec2', 'regions_exclude')
        if (configRegions == 'all'):
            if self.eucalyptus_host:
                self.regions.append(boto.connect_euca(host=self.eucalyptus_host).region.name)
            else:
                for regionInfo in ec2.regions():
                    if regionInfo.name not in configRegions_exclude:
                        self.regions.append(regionInfo.name)
        else:
            self.regions = configRegions.split(",")

        # Destination addresses
        self.destination_variable = config.get('ec2', 'destination_variable')
        self.vpc_destination_variable = config.get('ec2', 'vpc_destination_variable')

        # Cache related
        cache_path = config.get('ec2', 'cache_path')
        self.cache_path_cache = cache_path + "/ansible-ec2.cache"
        self.cache_path_index = cache_path + "/ansible-ec2.index"
        self.cache_max_age = config.getint('ec2', 'cache_max_age')

        # Optional keys for filtering on, expanding and formatting tags/values
        if config.has_option('ec2', 'list_value_tags'):
            self._load_list_value_tags(config.get('ec2', 'list_value_tags'))

        if config.has_option('ec2', 'tag_to_group'):
            self._load_tag_to_group(config.get('ec2', 'tag_to_group'))

        if config.has_option('ec2', 'include_hosts_by_tag'):
            self._load_include_hosts_by_tag(
                config.get('ec2', 'include_hosts_by_tag'))

    def _load_list_value_tags(self, raw):
        ''' Process the value of the list_value_tags setting as supplied in raw
        '''
        self.tag_processors['list_value_tags'] = \
            [s.strip() for s in raw.split(',')]
       
    def _load_tag_to_group(self, raw):
        ''' Process the value of the tag_to_group setting '''
        # remove all leading/trailing whitespace and ensure no empty
        # values as may be if a trailing ';' is left in
        units = [u.strip() for u in raw.split(';') if u.strip()]
        self.tag_processors['tag_to_group'] = \
            dict([u.split(':') for u in units]) 

    def _load_include_hosts_by_tag(self, raw):
        ''' Process the value of the include_hosts_by_tag settings '''
        # don't use split because ':' may appear in the value
        split_index = raw.find(':')
        if split_index < 1:
            raise Exception("Invalid argument to include_hosts_by_tag "
                            "setting: {0}".format(raw))
        key = raw[:split_index]
        value = raw[split_index+1:]

        self.tag_processors['include_hosts_by_tag'] = dict(
            key = key,
            value = value
            )
        
    def parse_environment(self):
        ''' Parse the environment for names prefixed INV_ and use as overrides.

        Thus INV_DESTINATION_VARIABLE will override destination_variable
        as set in the ini file.
        '''
        overrides = dict([(k.lower()[4:], os.environ[k])
                          for k in os.environ.keys() if k.startswith('INV_')])

        # we only assign known, safe names!
        for name in ['destination_variable',
                     'vpc_destination_variable',
                     'cache_path']:
            if name in overrides:
                setattr(self, name, overrides[name])

        regions_exclude = set()
        if 'regions_exclude' in overrides:
            regions_exclude = set(overrides['regions_exclude'].split(','))

        if 'regions' in overrides:
            regions = set(overrides['regions'].split(','))
            regions -= regions_exclude
            self.regions=list(regions)

        if 'cache_max_age' in overrides:
            self.cache_max_age = int(overrides['cache_max_age'])

        if 'list_value_tags' in overrides:
            self._load_list_value_tags(overrides['list_value_tags'])

        if 'tag_to_group' in overrides:
            self._load_tag_to_group(overrides['tag_to_group'])

        if 'include_hosts_by_tag' in overrides:
            self._load_include_hosts_by_tag(overrides['include_hosts_by_tag'])

    def parse_cli_args(self):
        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on EC2')
        parser.add_argument('--list', action='store_true', default=True,
                           help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                           help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                           help='Force refresh of cache by making API requests to EC2 (default: False - use cache files)')
        self.args = parser.parse_args()

    def do_api_calls_update_cache(self):
        ''' Do API calls to each region, and save data in cache files '''

        for region in self.regions:
            self.get_instances_by_region(region)
            self.get_rds_instances_by_region(region)

        if self.tag_processors['include_hosts_by_tag']:
            self.filter_instances()

        self.write_to_cache(self.inventory, self.cache_path_cache)
        self.write_to_cache(self.index, self.cache_path_index)


    def filter_instances(self):
        '''
        Filter out instances that are not tagged by the pattern defined
        in include_hosts_by_tag setting
        '''
        hostmatch = self.tag_processors['include_hosts_by_tag']
        inventory_key = self._format_key(hostmatch['key'], 
                                         hostmatch['value'])
        valid_hosts = set(self.inventory[inventory_key])

        for group, hosts in self.inventory.items():
            selection = list(set(hosts).intersection(valid_hosts))
            if selection:
                self.inventory[group] =  selection
            else:
                del(self.inventory[group])


    def get_instances_by_region(self, region):
        ''' Makes an AWS EC2 API call to the list of instances in a particular
        region '''

        try:
            if self.eucalyptus:
                conn = boto.connect_euca(host=self.eucalyptus_host)
                conn.APIVersion = '2010-08-31'
            else:
                conn = ec2.connect_to_region(region)

            # connect_to_region will fail "silently" by returning None if the region name is wrong or not supported
            if conn is None:
                print("region name: %s likely not supported, or AWS is down.  connection to region failed." % region)
                sys.exit(1)
 
            reservations = conn.get_all_instances()
            for reservation in reservations:
                for instance in reservation.instances:
                    self.add_instance(instance, region)
        
        except boto.exception.BotoServerError as e:
            if  not self.eucalyptus:
                print "Looks like AWS is down again:"
            print e
            sys.exit(1)

    def get_rds_instances_by_region(self, region):
	''' Makes an AWS API call to the list of RDS instances in a particular
        region '''

        try:
            conn = rds.connect_to_region(region)
            if conn:
                instances = conn.get_all_dbinstances()
                for instance in instances:
                    self.add_rds_instance(instance, region)
        except boto.exception.BotoServerError as e:
            print "Looks like AWS RDS is down: "
            print e
            sys.exit(1)

    def get_instance(self, region, instance_id):
        ''' Gets details about a specific instance '''
        if self.eucalyptus:
            conn = boto.connect_euca(self.eucalyptus_host)
            conn.APIVersion = '2010-08-31'
        else:
            conn = ec2.connect_to_region(region)

        # connect_to_region will fail "silently" by returning None if the region name is wrong or not supported
        if conn is None:
            print("region name: %s likely not supported, or AWS is down.  connection to region failed." % region)
            sys.exit(1)

        reservations = conn.get_all_instances([instance_id])
        for reservation in reservations:
            for instance in reservation.instances:
                return instance


    def add_instance(self, instance, region):
        ''' Adds an instance to the inventory and index, as long as it is
        addressable '''

        # Only want running instances
        if instance.state != 'running':
            return

        # Select the best destination address
        if instance.subnet_id:
            dest = getattr(instance, self.vpc_destination_variable)
        else:
            dest =  getattr(instance, self.destination_variable)

        if not dest:
            # Skip instances we cannot address (e.g. private VPC subnet)
            return

        # Add to index
        self.index[dest] = [region, instance.id]

        # Inventory: Group by instance ID (always a group of 1)
        self.inventory[instance.id] = [dest]

        # Inventory: Group by region
        self.push(self.inventory, region, dest)

        # Inventory: Group by availability zone
        self.push(self.inventory, instance.placement, dest)
        
        # Inventory: Group by instance type
        self.push(self.inventory, self.to_safe('type_' + instance.instance_type), dest)
        
        # Inventory: Group by key pair
        if instance.key_name:
            self.push(self.inventory, self.to_safe('key_' + instance.key_name), dest)
        
        # Inventory: Group by security group
        try:
            for group in instance.groups:
                key = self.to_safe("security_group_" + group.name)
                self.push(self.inventory, key, dest)
        except AttributeError:
            print 'Package boto seems a bit older.'
            print 'Please upgrade boto >= 2.3.0.'
            sys.exit(1)

        # Inventory: Group by tag keys
        for tag, v in instance.tags.iteritems():
            values = self._expand_tag_values(tag, v)
            keys = [self._format_key(tag, v) for v in values]
            
            for key in keys:
                self.push(self.inventory, key, dest)

    def _expand_tag_values(self, tag, v):
        ''' Expand tag values if tag is listed in list_value_tags setting
        Returns list containing either just the original value or, if to
        be expanded, the decomposed list.
        '''
        if tag in self.tag_processors['list_value_tags']:
            return v.split(',')
        return [v]

    def _format_key(self, tag, value):
        ''' Format key value as defined in tag_to_group if the tag has
        a custom formatter, otherwise format as tag_<key>_<value>.
        '''
        if tag in self.tag_processors['tag_to_group']:
            return self.to_safe(
                self.tag_processors['tag_to_group'][tag].format(
                    key=tag, value=value)
                )
        else:
            return self.to_safe('tag_{0}_{1}'.format(tag, value))

    def add_rds_instance(self, instance, region):
        ''' Adds an RDS instance to the inventory and index, as long as it is
        addressable '''

        # Only want available instances
        if instance.status != 'available':
            return

        dest = instance.endpoint[0]

        if not dest:
            # Skip instances we cannot address (e.g. private VPC subnet)
            return

        # Add to index
        self.index[dest] = [region, instance.id]

        # Inventory: Group by instance ID (always a group of 1)
        self.inventory[instance.id] = [dest]

        # Inventory: Group by region
        self.push(self.inventory, region, dest)

        # Inventory: Group by availability zone
        self.push(self.inventory, instance.availability_zone, dest)
        
        # Inventory: Group by instance type
        self.push(self.inventory, self.to_safe('type_' + instance.instance_class), dest)
        
        # Inventory: Group by security group
        try:
            if instance.security_group:
                key = self.to_safe("security_group_" + instance.security_group.name)
                self.push(self.inventory, key, dest)
        except AttributeError:
            print 'Package boto seems a bit older.'
            print 'Please upgrade boto >= 2.3.0.'
            sys.exit(1)

        # Inventory: Group by engine
        self.push(self.inventory, self.to_safe("rds_" + instance.engine), dest)

        # Inventory: Group by parameter group
        self.push(self.inventory, self.to_safe("rds_parameter_group_" + instance.parameter_group.name), dest)


    def get_host_info(self):
        ''' Get variables about a specific host '''

        if len(self.index) == 0:
            # Need to load index from cache
            self.load_index_from_cache()

        if not self.args.host in self.index:
            # try updating the cache
            self.do_api_calls_update_cache()
            if not self.args.host in self.index:
                # host migh not exist anymore
                return self.json_format_dict({}, True)

        (region, instance_id) = self.index[self.args.host]

        instance = self.get_instance(region, instance_id)
        instance_vars = {}
        for key in vars(instance):
            value = getattr(instance, key)
            key = self.to_safe('ec2_' + key)

            # Handle complex types
            if type(value) in [int, bool]:
                instance_vars[key] = value
            elif type(value) in [str, unicode]:
                instance_vars[key] = value.strip()
            elif type(value) == type(None):
                instance_vars[key] = ''
            elif key == 'ec2_region':
                instance_vars[key] = value.name
            elif key == 'ec2_tags':
                for k, v in value.iteritems():
                    key = self.to_safe('ec2_tag_' + k)
                    instance_vars[key] = v
            elif key == 'ec2_groups':
                group_ids = []
                group_names = []
                for group in value:
                    group_ids.append(group.id)
                    group_names.append(group.name)
                instance_vars["ec2_security_group_ids"] = ','.join(group_ids)
                instance_vars["ec2_security_group_names"] = ','.join(group_names)
            else:
                pass
                # TODO Product codes if someone finds them useful
                #print key
                #print type(value)
                #print value

        return self.json_format_dict(instance_vars, True)


    def push(self, my_dict, key, element):
        ''' Pushed an element onto an array that may not have been defined in
        the dict '''

        if key in my_dict:
            my_dict[key].append(element);
        else:
            my_dict[key] = [element]


    def get_inventory_from_cache(self):
        ''' Reads the inventory from the cache file and returns it as a JSON
        object '''

        cache = open(self.cache_path_cache, 'r')
        json_inventory = cache.read()
        return json_inventory


    def load_index_from_cache(self):
        ''' Reads the index from the cache file sets self.index '''

        cache = open(self.cache_path_index, 'r')
        json_index = cache.read()
        self.index = json.loads(json_index)


    def write_to_cache(self, data, filename):
        ''' Writes data in JSON format to a file '''

        json_data = self.json_format_dict(data, True)
        cache = open(filename, 'w')
        cache.write(json_data)
        cache.close()


    def to_safe(self, word):
        ''' Converts 'bad' characters in a string to underscores so they can be
        used as Ansible groups '''

        return re.sub("[^A-Za-z0-9\-]", "_", word)


    def json_format_dict(self, data, pretty=False):
        ''' Converts a dict to a JSON object and dumps it as a formatted
        string '''

        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)


# Run the script
Ec2Inventory()

