'''
Module to create COSBench performance testing environment for Ceph RADOS+RGW clusters and AWS S3 clusters.

On client machines:
==================
- Install COSBench and dependencies
- Deploy Ceph S3 COSBench workload configuration file
- Deploy AWS S3 COSBench workload configuration file
- Designate first client as test controller.
- Configure COSBench controller.conf with details of all driver endpoints

Storage cluster:
===============
- 2 storage servers running OSD and RGW
- 1 node acting as MON and admin
- Create 2 768GB storage servers with 744GB OSD, 8GB OSD journal, 8GB boot, 8GB swap
- Create 1 2GB node for MON and admin
- Configure hostnames and /etc/hosts for all nodes
- Configure public key SSH access from admin node to all other nodes (passwordless sudo not necessary because SSH will be root user)
- Configure NTP on all nodes
- Configure Ceph repo on admin node, and install ceph-deploy
- Create RADOS cluster by runing ceph-deploy create
- Add osd pool size and RGW client sections to conf file
- ceph-deploy install on all nodes, prepare OSD and activate OSD steps
- Push conf files to entire cluster
- Create RGW nodes by ceph-deploy rgw
- Create S3 test user and obtain access key and secret key
- Configure Linode NodeBalancer to balance between the RGW hosts
- Create a COSBench workload file configured to access this cluster

NodeBalancer configuration for the 2 RGW endpoints:
- nodebalancer.create
- nodebalancer.config.create
- nodebalancer.node.create - once for each RGW endpoint
'''

from __future__ import print_function

import os
import os.path
import collections

from linode_core import Core, Linode
from provisioners import AnsibleProvisioner

import simplejson as json

import logger


def create_cluster(name, datacenter):
    
    test_cluster = load_cluster(name)
    if test_cluster is not None:
        print('Cluster %s already exists. Use a different name or load this one instead of creating.' % (name))
        return None
        
    cluster = collections.OrderedDict()
    cluster['name'] = name
    cluster['dc'] = datacenter
    cluster['admin'] = {}
    cluster['monitors'] = []
    cluster['servers'] = []
    cluster['clients'] = []
    
    save_cluster(cluster)
        
    return cluster
    
    
def add_admin_mon_node(name):
    
    cluster = load_cluster(name)
    
    '''
    Create a machine that acts as both Ceph admin node and Ceph MONitor node
    to deploy and manage Ceph cluster.
    '''
    # Create a linode
    app_ctx = {'conf-dir' : conf_dir()}
    core = Core(app_ctx)
    
    label = 'cephadmin'
    admin_linode_spec = {
            'plan_id' : 1,
            'datacenter' : cluster['dc'],
            'distribution' : 'Ubuntu 14.04 LTS',
            'kernel' : 'Latest 64 bit',
            'label' : label,
            'group' : 'cephperftests',
            'disks' :   {
                            'boot' : {'disk_size' : 22*1024},
                            'swap' : {'disk_size' : 2048}
                        }

    }
        
    linode = core.create_linode(admin_linode_spec)
    if not linode:
        logger.error_msg('Could not create admin node')
        return None
    
    # Save admin node details to cluster under both admin and monitor node keys.
    admin = collections.OrderedDict()
    admin['id'] = linode.id
    admin['public_ip'] = str(linode.public_ip[0])
    admin['private_ip'] = linode.private_ip
    admin['fqdn'] = 'cephadmin.' + cluster['name']
    admin['shortname'] = 'cephadmin' # ceph-deploy seems to use the short name for some reason.
    
    cluster['admin'] = admin
    
    # Though same node is acting as the lone monitor, treat it as if it's 
    # a separate node and add it to list of monitors.
    monitor = admin.copy()
    monitor['fqdn'] = 'cephmon1.' + cluster['name']
    monitor['shortname'] = 'cephmon1'
    cluster['monitors'] = [ monitor ]
    
    save_cluster(cluster)
    
    #provision_admin_mon(cluster, admin)

def add_mon_node(name):
    
    cluster = load_cluster(name)
    
    '''
    Create a machine that acts as both Ceph admin node and Ceph MONitor node
    to deploy and manage Ceph cluster.
    '''
    # Create a linode
    app_ctx = {'conf-dir' : conf_dir()}
    core = Core(app_ctx)
    
    label = 'cephmon1'
    mon_linode_spec = {
            'plan_id' : 1,
            'datacenter' : cluster['dc'],
            'distribution' : 'Ubuntu 14.04 LTS',
            'kernel' : 'Latest 64 bit',
            'label' : label,
            'group' : 'cephperftests',
            'disks' :   {
                            'boot' : {'disk_size' : 22*1024},
                            'swap' : {'disk_size' : 2048}
                        }

    }
        
    linode = core.create_linode(mon_linode_spec)
    if not linode:
        logger.error_msg('Could not create mon node')
        return None
    
    # Save admin node details to cluster under both admin and monitor node keys.
    mon = collections.OrderedDict()
    mon['id'] = linode.id
    mon['public_ip'] = str(linode.public_ip[0])
    mon['private_ip'] = linode.private_ip
    mon['fqdn'] = 'cephmon1.' + cluster['name']
    mon['shortname'] = 'cephmon1' # ceph-deploy seems to use the short name for some reason.
    
    cluster['monitors'] = [ mon ]
    
    save_cluster(cluster)



def provision_admin_mon(cluster, admin):    
    print(cluster)
    print(admin)
    
    prov = AnsibleProvisioner()
    
    admin_ip = admin['public_ip']
    
    # Wait for SSH service on linode to come up.
    temp = Linode()
    temp.public_ip = [ admin_ip ]
    if not prov.wait_for_ping(temp, 60, 10):
        print("Unable to reach %s over SSH" % (admin_ip))
        return None
    
    print('Provisioning admin node')
    
    # Set the node's hostname. No underscrores allowed in hostname.
    admin['hostname'] = 'cephadminmon'
    prov.exec_playbook(admin_ip, 'ansible/change_hostname.yaml',
        variables = {
            'new_hostname' : admin['hostname']
        })
        
    # Update /etc/hosts. this is required here before cluster creation
    # because mon node's FQDN should be added to known_hosts and to resolve that FQDN,
    # /etc/hosts should already have that entry.
    update_storage_fqdn_entries(cluster)
        
    # Provision admin node's public key. Configure it to allow only key based
    # SSH. Provision ceph-deploy on admin node.
    # This playbook also fetches admin's public key and saves it in conf_dir/<CLUSTER>/pubkeys/<FQDN>.pub
    # While sending paths to ansible, always send absolute paths, because ansible's working 
    # directory is the directory in which the playbook resides, not the directory from which
    # the ansible-playbook is executed.
    pubkey_dir = os.path.join(conf_dir(), cluster['name'], 'pubkeys') 
    if not os.path.exists(pubkey_dir):
        os.makedirs(pubkey_dir)
    
    pubkey_file = os.path.abspath( os.path.join(pubkey_dir, admin['fqdn'] + '.pub' ) )
    prov.exec_playbook(admin_ip, 'ansible/ceph_admin_node.yaml',
        variables = {
            # If path does not end with a /, this becomes the name of the downloaded file
            # instead of the directory under which it should be saved.
            'local_pubkey_file' : pubkey_file
        })
    
    if os.path.isfile(pubkey_file):
        with open(pubkey_file, 'r') as f:
            pubkey = f.read().strip('\n')
            
        admin['pubkey'] = pubkey
        
        mon = cluster['monitors'][0]
        mon['pubkey'] = pubkey
        with open(os.path.join(pubkey_dir, mon['fqdn'] + '.pub' ), 'w') as f:
            f.write(pubkey)
            
        save_cluster(cluster)
        
    else:
        print('Error: public key %s not found' % (pubkey_file))
    
    
    # The admin node should be able to SSH to itself, because it's also
    # acting as MON node.    
    prov.exec_playbook(admin_ip, 'ansible/add_authorized_keys.yaml',
        variables = {
            'keys' : admin['pubkey'] + '\n',
            'cluster' : cluster['name']
        })

    # Create Ceph initial cluster conf, and add the MON FQDN to admin node's known_hosts. This should be done
    # even though both nodes are the same.
    prov.exec_playbook(admin_ip, 'ansible/create_cluster.yaml',
        variables = {
            'mon' : mon,
            'cluster_name' : cluster['name']
        })


def add_client(name):
    
    cluster = load_cluster(name)
    
    '''
    Create a machine that acts as Ceph client
    and runs perf tests.
    '''
    # Create a linode
    app_ctx = {'conf-dir' : conf_dir()}
    core = Core(app_ctx)
    
    client_index = len(cluster['clients']) + 1
    
    label = 'perfclient-%d' % (client_index)
    client_linode_spec = {
            'plan_id' : 1,
            'datacenter' : cluster['dc'],
            'distribution' : 'Ubuntu 14.04 LTS',
            'kernel' : 'Latest 64 bit',
            'label' : label,
            'group' : 'perftests',
            #'disks' :   {
                            #'boot' : {'disk_size' : 19000},
                            #'swap' : {'disk_size' : 'auto'}
                        #}
            'disks' :   {
                            'boot' : {'disk_size' : 2.5*1024},
                            'swap' : {'disk_size' : 512},
                            'others' :  [
                                        {
                                            'label' : 'testhd',
                                            'disk_size' : 20 * 1024,
                                            'type' : 'xfs'
                                        }
                                        ]
                        }

    }
        
    linode = core.create_linode(client_linode_spec)
    if not linode:
        logger.error_msg('Could not create perf client')
        return
    
    # Save client details to cluster.
    client = collections.OrderedDict()
    client['id'] = linode.id
    client['public_ip'] = str(linode.public_ip[0])
    client['private_ip'] = linode.private_ip
    
    cluster['clients'].append(client)
    save_cluster(cluster)
    
    provision_client(cluster, client)




def provision_client(cluster, client):    
    print(cluster)
    print(client)
    
    # Provision it with glusterfs client, perf tools and monitoring tools.
    prov = AnsibleProvisioner()
    
    # Wait for SSH service on linode to come up.
    temp = Linode()
    temp.public_ip = [ client['public_ip'] ]
    if not prov.wait_for_ping(temp, 60, 10):
        print("Unable to reach %s over SSH" % (client['public_ip']))
        return
    
    pubkey_dir = os.path.join(conf_dir(), str(client['id'])) 
    if not os.path.exists(pubkey_dir):
        os.makedirs(pubkey_dir)
    
    print('Provisioning client')
    
    # Provision client's public key. Configure it to allow only key based
    # SSH. Provision cluster and perf tools on client.
    # This playbook also fetches client's public key and saves it in conf_dir/<LINODE_ID>/id_rsa.pub
    # While sending paths to ansible, always send absolute paths, because ansible's working 
    # directory is the directory in which the playbook resides, not the directory from which
    # the ansible-playbook is executed.
    prov.exec_playbook(client['public_ip'], 'ansible/perf_client.yaml',
        variables = {
            # If path does not end with a /, this becomes the name of the downloaded file
            # instead of the directory under which it should be saved.
            'pubkey_dir' : os.path.abspath(pubkey_dir + '/')
        })
    
    pubkey_file = os.path.join(pubkey_dir, 'id_rsa.pub')
    if os.path.isfile(pubkey_file):
        with open(pubkey_file, 'r') as f:
            pubkey = f.read().strip('\n')
            
        client['pubkey'] = pubkey
        save_cluster(cluster)
        
    else:
        print('Error: public key %s not found' % (pubkey_file))
    
    # Authorize client to access all other machines in cluster, and
    # vice versa.
    other_keys = [ c['pubkey'] for c in cluster['clients'][:-1] ]
    server_keys = [ s['pubkey'] for s in cluster['servers'] ]
    other_keys.extend(server_keys)
    
    if other_keys:
        add_auth_keys_to_client = '\n\n' + '\n'.join(other_keys) + '\n'
        
        print('Adding authorized keys')
        prov.exec_playbook(client['public_ip'], 'ansible/add_authorized_keys.yaml',
            variables = {
                'keys' : add_auth_keys_to_client
            })
        
        # Now add this client's key to all other machines.
        targets = [ c['public_ip'] for c in cluster['clients'][:-1] ]
        targets.extend( [ s['public_ip'] for s in cluster['servers'] ] )
        prov.exec_playbook(targets, 'ansible/add_authorized_keys.yaml',
            variables = {
                'keys' : client['pubkey'] + '\n'
            })


def add_server(name):
    
    cluster = load_cluster(name)
    
    '''
    Create a machine that acts as both Ceph OSD node and Ceph RGW node.
    '''
    # Create a linode
    app_ctx = {'conf-dir' : conf_dir()}
    core = Core(app_ctx)
    
    server_index = len(cluster['servers']) + 1
    
    label = 'cephosdrgw-%d' % (server_index)
    
    server_linode_spec = {
            'plan_id' : 7, # Linode 384 GB storage, 2Mbps outgoing network
            'datacenter' : cluster['dc'],
            'distribution' : 'Ubuntu 14.04 LTS',
            'kernel' : 'Latest 64 bit',
            'label' : label,
            'group' : 'cephperftests',
            'disks' :   {
                            'boot' : {'disk_size' : 10*1024},
                            'swap' : {'disk_size' : 2*1024},
                            'others' :  [
                                        {
                                            'label' : 'osd',
                                            'disk_size' : 360 * 1024,
                                            'type' : 'xfs'
                                        },
                                        {
                                            'label' : 'osdjournal',
                                            'disk_size' : 12 * 1024,
                                            'type' : 'xfs'
                                        }
                                        
                                        ]
                            
                        }

    }
        
    linode = core.create_linode(server_linode_spec)
    if not linode:
        logger.error_msg('Could not create server node')
        return None
    
    # Save admin node details to cluster under both admin and monitor node keys.
    server = collections.OrderedDict()
    server['id'] = linode.id
    server['public_ip'] = str(linode.public_ip[0])
    server['private_ip'] = linode.private_ip
    server['fqdn'] = 'cephosdrgw%d.%s' % (server_index, cluster['name'])
    server['shortname'] = 'cephosdrgw%d' % (server_index) 
    
    cluster['servers'].append(server)
    
    save_cluster(cluster)
    
    
def provision_server(cluster, server):    
    
    # Provision it with basic security configuration. ceph-deploy takes
    # care of actual Ceph provisioning.
    prov = AnsibleProvisioner()
    
    server_ip = server['public_ip']
    
    # Wait for SSH service on linode to come up.
    temp = Linode()
    temp.public_ip = [ server_ip ]
    prov.wait_for_ping(temp, 60, 10)
    
    print('Provisioning server')
    
    prov.exec_playbook(server_ip, 'ansible/change_hostname.yaml',
        variables = {
            'new_hostname' : server['shortname']+'local'
        })
    
    pubkey_dir = os.path.join(conf_dir(), cluster['name'], 'pubkeys') 
    if not os.path.exists(pubkey_dir):
        os.makedirs(pubkey_dir)
    
    pubkey_file = os.path.abspath( os.path.join(pubkey_dir, server['fqdn'] + '.pub' ) )
    prov.exec_playbook(server_ip, 'ansible/ceph_server.yaml',
        variables = {
            # If path does not end with a /, this becomes the name of the downloaded file
            # instead of the directory under which it should be saved.
            'local_pubkey_file' : pubkey_file
        })
    
    if os.path.isfile(pubkey_file):
        with open(pubkey_file, 'r') as f:
            pubkey = f.read().strip('\n')
            
        server['pubkey'] = pubkey
        
        save_cluster(cluster)
        
    else:
        print('Error: public key %s not found' % (pubkey_file))
        
    # Authorized cephadmin for SSH access to this server. ceph-deploy requires it.
    prov.exec_playbook(server_ip, 'ansible/add_authorized_keys.yaml',
            variables = {
                'keys' : cluster['admin']['pubkey'] + '\n',
                'cluster' : cluster['name']
            })

    
    


def update_storage_fqdn_entries(cluster):
    
    # Update /etc/hosts on all nodes of storage cluster to include all nodes in storage cluster.
    # Client nodes are neither included nor updated, because clients access the cluster only via
    # Nodebalancer public IP.
    
    prov = AnsibleProvisioner()
    
    host_entries = []
    targets = []
    
    # Add entry for admin node.
    host_entries.append( 
        {   'ip' : cluster['admin']['private_ip'], 
            'fqdn' : cluster['admin']['fqdn'],
            'shortname' : cluster['admin']['shortname'] 
        })
    targets.append(cluster['admin']['public_ip'])


    # Add entries for monitor nodes.
    for mon in cluster['monitors']:
        host_entries.append( 
            {   'ip' : mon['private_ip'], 
                'fqdn' : mon['fqdn'],
                'shortname' : mon['shortname'] 
            })
            
        targets.append(mon['public_ip'])
    
    
    # Add entries for storage nodes.
    for server in cluster['servers']:
        host_entries.append( 
            {   'ip' : server['private_ip'], 
                'fqdn' : server['fqdn'],
                'shortname' : server['shortname'] 
            })
            
        targets.append(server['public_ip'])
    
    prov.exec_playbook(targets, 'ansible/modify_hosts_file.yaml',
        variables = {
            'host_entries' : host_entries,
            'cluster' : cluster['name']
        })
            
    
def load_cluster(name):
    
    the_conf_dir = conf_dir()
    cluster_file = os.path.join(the_conf_dir, name, name + '.json')
    if not os.path.isfile(cluster_file):
        return None

    with open(cluster_file, 'r') as f:
        cluster = json.load(f, object_pairs_hook=collections.OrderedDict)
    
    return cluster
    




def save_cluster(cluster):
    
    the_conf_dir = conf_dir()
    
    cluster_dir = os.path.join(the_conf_dir, cluster['name'])
    if not os.path.exists(cluster_dir):
        os.makedirs(cluster_dir)
    
    cluster_file = os.path.join(cluster_dir, cluster['name'] + '.json')
    with open(cluster_file, 'w') as f:
        json.dump(cluster, f, indent = 4 * ' ')        
    
    
    
def conf_dir() :
    return './cephperfdata'
    
    

        
        

if __name__ == '__main__':
    
    name = 'cephperfcluster'
    #name = 'vmtest'
    
    #cluster = create_cluster(name, 6) # 6 is Newark, NJ DC
    
    cluster = load_cluster(name)
    
    #admin = {'id' : 'vm1', 'public_ip' : '192.168.11.4', 'private_ip' : '192.168.11.4', 'fqdn' : 'cephadmin.' + name, 
        #'shortname' : 'cephadmin' }
    #mon1 = {'id' : 'vm1', 'public_ip' : '192.168.11.4', 'private_ip' : '192.168.11.4', 'fqdn' : 'cephmon1.' + name,
        #'shortname' : 'cephmon1' }
    #cluster['admin'] = admin
    #cluster['monitors'] = [ mon1 ]
    
    #add_admin_mon_node(name)
    
    #add_mon_node(name)
    
    #provision_admin_mon(cluster, cluster['admin'])
    
    #add_server(name)
    
    #provision_server(cluster, cluster['servers'][-1])
    
    # After all nodes are added, update /etc/hosts on all nodes.
    update_storage_fqdn_entries(cluster)
    
    #add_client(name)
    
    
