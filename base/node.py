import ipaddress
import json
import os
import shutil
import subprocess as sp
import threading
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from typing import Dict, List, Type
import zipfile

from flask import Flask, request

from .link import RealLink, VirtualLink
from .manager import Manager
from .nfs import Nfs
from .utils import read_json, send_data
from .scheduler.scheduler import Scheduler


class Worker(object):
	"""
	superclass of physical environment workers.
	make sure your worker have the Linux Traffic Control installed.
	it may be named iproute2 on apt or iproute on yum.
	we also recommend installing some useful tools,
	like net-tools, iperf3, iputils-ping and nano.
	"""

	def __init__(self, ID: int, name: str, ip: str):
		self.idW: int = ID
		self.nameW: str = name
		self.ipW: str = ip
		self.connected: Dict[int, List[int]]  # dst worker ID to real link ID set.

	def check_network_range(self, network: str):
		subnet = ipaddress.ip_network(network, strict=False)
		subnet_start, subnet_end = [x for x in subnet.hosts()][0], [x for x in subnet.hosts()][-1]
		assert subnet_start <= ipaddress.ip_address(self.ipW) <= subnet_end, Exception(
			self.ipW + ' is not in the subnet of ' + network)


class Node(object):
	"""
	superclass of test environment nodes.
	"""

	def __init__(self, ID: int, name: str, ip: str, nic: str, working_dir: str,
				 cmd: List[str], dml_port: int, host_port: int):
		self.id: int = ID
		self.name: str = name
		self.ip: str = ip
		self.nic: str = nic
		self.workingDir: str = working_dir
		self.cmd: List[str] = cmd
		self.dmlPort: int = dml_port  # dml application listens on $(dml_port).
		self.hostPort: int = host_port  # network requests are sent to $(host_port) of worker.
		self.variable: Dict[str, str] = {}  # system environment variable.
		# TODO Class VirtualLink
		self.tc: Dict[str, str] = {}  # dst name to dst bw.
		self.tcIP: Dict[str, str] = {}  # dst name to dst ip.
		self.tcPort: Dict[str, int] = {}  # dst name to dst host port.

		self.add_var({
			'EDGE_TB_ID': str(ID),
			'NET_NODE_NAME': name,
			'DML_PORT': str(dml_port)
		})

	def add_var(self, var_dict: Dict[str, str]):
		self.variable.update(var_dict)

	def link_to(self, name: str, bw: str, ip: str, port: int):
		assert name not in self.tc, Exception(self.name + ' already has a link to ' + name)
		self.tc[name] = bw
		self.tcIP[name] = ip
		self.tcPort[name] = port


class PhysicalNode(Node, Worker):
	"""
	a physical node represented by a worker.
	"""

	def __init__(self, WID: int, NID: int, name: str, nic: str, ip: str, dml_port: int):
		# use the same host port as dml port.
		Node.__init__(self, NID, name, ip, nic, '', [], dml_port, dml_port)
		Worker.__init__(self, WID, name, ip)
		self.nfsMount: Dict[str, str] = {}  # nfs path to node path.

		self.add_var({'NET_NODE_NIC': nic})

	def mount_nfs(self, nfs: Nfs, mount_point: str):
		self.check_network_range(nfs.subnet)
		self.nfsMount[nfs.path] = mount_point

	def set_cmd(self, working_dir: str, cmd: List[str]):
		assert self.workingDir == '' and not self.cmd, Exception('cmd has been set')
		self.workingDir = working_dir
		self.cmd.extend(cmd)


class EmulatedNode(Node):
	"""
	an emulated node represented by a docker container and deployed on a worker (emulator).
	"""

	def __init__(self, ID: int, name: str, nic: str, working_dir: str, cmd: List[str], dml_port: int,
				 base_host_port: int, image: str, cpu: int, ram: int):
		# host port is related to node's id, dml port maps to host port in emulator.
		super().__init__(ID, name, '', nic, working_dir, cmd, dml_port, base_host_port + ID)
		self.image: str = image  # Docker image.
		self.cpu = cpu  # cpu thread.
		self.ram = ram  # MB of memory.
		self.volume: Dict[str, str] = {}  # host path or nfs tag to node path.

	def mount_local_path(self, local_path: str, node_path: str):
		assert node_path[0] == '/', Exception(node_path + ' is not an absolute path')
		self.volume[local_path] = node_path

	def mount_nfs(self, nfs: Nfs, node_path: str):
		assert node_path[0] == '/', Exception(node_path + ' is not an absolute path')
		self.volume[nfs.tag] = node_path + '/:ro'


class Emulator(Worker):
	"""
	a worker that can deploy multiple emulated nodes.
	"""

	def __init__(self, ID: int, name: str, ip: str, cpu: int, ram: int, ip_testbed: str):
		super().__init__(ID, name, ip)
		self.cpu: int = cpu  # cpu thread.
		self.ram: int = ram  # MB of memory.
		self.ipTestbed: str = ip_testbed  # ip of the testbed controller.
		self.cpuPreMap: int = 0  # allocated cpu.
		self.ramPreMap: int = 0  # allocated ram.
		self.nfs: List[Nfs] = []  # mounted nfs.
		self.eNode: Dict[str, EmulatedNode] = {}  # emulated node's name to emulated node object.
		self.curr_cpu: int = 0	# 服务器目前的cpuId

	def mount_nfs(self, nfs: Nfs):
		assert nfs not in self.nfs, Exception(nfs.tag + ' has been mounted')
		self.check_network_range(nfs.subnet)
		self.nfs.append(nfs)

	def check_resource(self, name: str, cpu: int, ram: int):
		assert self.cpu - self.cpuPreMap >= cpu and self.ram - self.ramPreMap >= ram, Exception(
			self.nameW + '\'s cpu or ram is not enough for ' + name)

	def add_node(self, en: EmulatedNode):
		assert en.name not in self.eNode, Exception(en.name + ' has been added')
		en.ip = self.ipW
		self.cpuPreMap += en.cpu
		self.ramPreMap += en.ram
		self.eNode[en.name] = en

	def delete_node(self, en: EmulatedNode):
		assert en.name in self.eNode, Exception(en.name + ' is not existed')
		self.cpuPreMap -= en.cpu
		self.ramPreMap -= en.ram
		del self.eNode[en.name]

	def save_yml(self, path: str):
		# 保存yml
		if not self.eNode:
			return
		str_yml = 'version: "2.1"\n'
		if self.nfs:
			str_yml += 'volumes:\n'
			for nfs in self.nfs:
				str_yml = str_yml \
						  + '  ' + nfs.tag + ':\n' \
						  + '    driver_opts:\n' \
						  + '      type: "nfs"\n' \
						  + '      o: "addr=' + self.ipTestbed + ',ro"\n' \
						  + '      device: ":' + nfs.path + '"\n'
		curr_cpu = 0
		str_yml += 'services:\n'
		for en in self.eNode.values():
			str_yml = str_yml \
					  + '  ' + en.name + ':\n' \
					  + '    container_name: ' + en.name + '\n' \
					  + '    image: ' + en.image + '\n' \
					  + '    working_dir: ' + en.workingDir + '\n' \
					  + '    stdin_open: true\n' \
					  + '    tty: true\n' \
					  + '    cap_add:\n' \
					  + '      - NET_ADMIN\n' \
					  + '    cpuset: ' + str(curr_cpu) + '-' + str(curr_cpu + en.cpu - 1) + '\n' \
					  + '    mem_limit: ' + str(en.ram) + 'M\n'
			curr_cpu += en.cpu
			str_yml += '    environment:\n'
			for key in en.variable:
				str_yml += '      - ' + key + '=' + en.variable[key] + '\n'
			str_yml = str_yml \
					  + '    healthcheck:\n' \
					  + '      test: curl -f http://localhost:' + str(en.dmlPort) + '/hi\n'
			str_yml = str_yml \
					  + '    ports:\n' \
					  + '      - "' + str(en.hostPort) + ':' + str(en.dmlPort) + '"\n'
			if en.volume:
				str_yml += '    volumes:\n'
				for v in en.volume:
					str_yml += '      - ' + v + ':' + en.volume[v] + '\n'
			if en.cmd:
				str_yml += '    command: ' + ' '.join(en.cmd) + '\n'

		# save as yml file
		yml_name = os.path.join(path, self.nameW + '.yml')
		with open(yml_name, 'w') as f:
			f.writelines(str_yml)


	def save_yml_user(self, path: str, taskId: int):
		# 保存yml
		if not self.eNode:
			return
		str_yml = 'version: "2.1"\n'
		if self.nfs:
			str_yml += 'volumes:\n'
			for nfs in self.nfs:
				str_yml = str_yml \
						  + '  ' + nfs.tag + ':\n' \
						  + '    driver_opts:\n' \
						  + '      type: "nfs"\n' \
						  + '      o: "addr=' + self.ipTestbed + ',ro"\n' \
						  + '      device: ":' + nfs.path + '"\n'
		self.curr_cpu = 0
		str_yml += 'services:\n'
		for en in self.eNode.values():
			str_yml = str_yml \
					  + '  ' + en.name + ':\n' \
					  + '    container_name: ' + en.name + '\n' \
					  + '    image: ' + en.image + '\n' \
					  + '    working_dir: ' + en.workingDir + '\n' \
					  + '    stdin_open: true\n' \
					  + '    tty: true\n' \
					  + '    cap_add:\n' \
					  + '      - NET_ADMIN\n' \
					  + '    cpuset: ' + str(self.curr_cpu) + '-' + str(self.curr_cpu + en.cpu - 1) + '\n' \
					  + '    mem_limit: ' + str(en.ram) + 'M\n'
			self.curr_cpu += en.cpu
			str_yml += '    environment:\n'
			for key in en.variable:
				str_yml += '      - ' + key + '=' + en.variable[key] + '\n'
			str_yml = str_yml \
					  + '    healthcheck:\n' \
					  + '      test: curl -f http://localhost:' + str(en.dmlPort) + '/hi\n'
			str_yml = str_yml \
					  + '    ports:\n' \
					  + '      - "' + str(en.hostPort) + ':' + str(en.dmlPort) + '"\n'
			if en.volume:
				str_yml += '    volumes:\n'
				for v in en.volume:
					str_yml += '      - ' + v + ':' + en.volume[v] + '\n'
			if en.cmd:
				str_yml += '    command: ' + ' '.join(en.cmd) + '\n'

		# save as yml file
		yml_name = os.path.join(path, self.nameW + '_' + taskId + '.yml')
		with open(yml_name, 'w') as f:
			f.writelines(str_yml)
	

class Testbed(object):
	"""
	testbed controller.
	"""

	def __init__(self, ip: str, base_host_port: int, dir_name: str, manager_class: Type[Manager],  scheduler: Scheduler):
		self.currWID: int = 0  # build-in worker ID.
		self.currRID: int = 0  # build-in real link ID.
		self.currNID: int = 0  # build-in node ID.
		self.currVID: int = 0  # build-in virtual link ID.

		self.flask = Flask(__name__)
		self.ip: str = ip
		self.port: int = 3333  # DO NOT change this port number.
		self.agentPort: int = 3333  # DO NOT change this port number.
		self.dmlPort: int = 4444  # DO NOT change this port number.
		# emulated node maps dml port to emulator's host port starting from $(base_host_port).
		self.hostPort: int = base_host_port
		self.address: str = self.ip + ':' + str(self.port)
		self.dirName: str = dir_name

		self.nfs: Dict[str, Nfs] = {}  # nfs tag to nfs object.
		self.pNode: Dict[str, PhysicalNode] = {}  # physical node's name to physical node object.
		self.emulator: Dict[str, Emulator] = {}  # emulator's name to emulator object.
		self.eNode: Dict[str, EmulatedNode] = {}  # emulated node's name to emulated node object.
		self.rLink: Dict[int, RealLink] = {}  # real link ID to real link object.
		self.vLink: Dict[int, VirtualLink] = {}  # virtual link ID to virtual link object.
		self.virtualLinkNumber: int = 0

		# for auto deployment.
		self.W: Dict[int, Dict] = {}  # worker ID to {name, cpu, MB of ram}.
		self.N: Dict[int, Dict] = {}  # node ID to {name, cpu, MB of ram}.
		self.RConnect: List[List[List[int]]]  # workers adjacency matrix.
		self.VConnect: List[List[List[int]]]  # nodes adjacency matrix.
		self.preMap: Dict[int, int] = {}  # node ID to worker ID.

		# for default manager.
		self.manager = manager_class(self)
		self.deployedCount: int = 0
		self.lock = threading.RLock()
		self.executor = ThreadPoolExecutor()
		# scheduler
		self.scheduler = scheduler(self)

	def __next_w_id(self):
		self.currWID += 1
		return self.currWID

	def __next_r_id(self):
		self.currRID += 1
		return self.currRID

	def __next_n_id(self):
		self.currNID += 1
		return self.currNID

	def __next_v_id(self):
		self.currVID += 1
		return self.currVID

	def add_nfs(self, tag: str, path: str, ip: str = '', mask: int = 16) -> Nfs:
		assert tag != '', Exception('tag cannot be empty')
		assert tag not in self.nfs, Exception(tag + ' has been used')
		assert 0 < mask <= 32, Exception(str(mask) + ' is not in range (0, 32]')
		assert path[0] == '/', Exception(path + ' is not an absolute path')
		if ip == '':
			ip = self.ip
		nfs = Nfs(tag, path, ip, mask)
		self.nfs[tag] = nfs
		return nfs

	def add_emulator(self, name: str, ip: str, cpu: int, ram: int, unit: str) -> Emulator:
		assert name != '', Exception('name cannot be empty')
		assert name not in self.emulator, Exception(name + ' has been used')
		assert cpu > 0 and ram > 0, Exception('cpu or ram is not bigger than 0')
		assert unit in ['M', 'G'], Exception(unit + ' is not in ["M", "G"]')
		if unit == 'G':
			ram *= 1024
		wid = self.__next_w_id()
		e = Emulator(wid, name, ip, cpu, ram, self.ip)
		self.emulator[name] = e
		for tag in self.nfs.values():  # mount all nfs tags by default.
			e.mount_nfs(tag)

		self.W[wid] = {'name': name, 'cpu': cpu, 'ram': ram}
		return e

	def add_physical_node(self, name: str, nic: str, ip: str) -> PhysicalNode:
		assert name != '', Exception('name cannot be empty')
		assert name not in self.eNode, Exception(name + ' has been used')
		assert name not in self.pNode, Exception(name + ' has been used')
		wid = self.__next_w_id()
		nid = self.__next_n_id()
		pn = PhysicalNode(wid, nid, name, nic, ip, self.dmlPort)
		self.pNode[name] = pn

		self.W[wid] = {'name': name, 'cpu': 0, 'ram': 0}
		self.N[nid] = {'name': name, 'cpu': 0, 'ram': 0}
		pn.add_var({
			'NET_CTL_ADDRESS': self.address,
			'NET_AGENT_ADDRESS': ip + ':' + str(self.agentPort)
		})
		self.preMap[nid] = wid
		return pn

	def add_emulated_node(self, name: str, working_dir: str, cmd: List[str], image: str,
						  cpu: int, ram: int, unit: str, nic: str = 'eth0', emulator: Emulator = None) -> EmulatedNode:
		assert name != '', Exception('name cannot be empty')
		assert name not in self.eNode, Exception(name + ' has been used')
		assert name not in self.pNode, Exception(name + ' has been used')
		assert cpu > 0 and ram > 0, Exception('cpu or ram is not bigger than 0')
		assert unit in ['M', 'G'], Exception(unit + ' is not in ["M", "G"]')
		if unit == 'G':
			ram *= 1024

		if emulator:
			emulator.check_resource(name, cpu, ram)

		nid = self.__next_n_id()
		en = EmulatedNode(nid, name, nic, working_dir, cmd, self.dmlPort, self.hostPort, image, cpu, ram)

		if emulator:
			self.assign_emulated_node(en, emulator)

		self.eNode[name] = en
		self.N[nid] = {'name': name, 'cpu': cpu, 'ram': ram}
		return en

	def delete_emulated_node(self, en: EmulatedNode, emulator: Emulator = None):
		assert en.name in self.eNode, Exception(en.name + ' is not existed')
		if emulator:
			emulator.delete_node(en)
		del self.eNode[en.name]
		del self.N[en.id]
		del self.preMap[en.id]

	def assign_emulated_node(self, en: EmulatedNode, emulator: Emulator):
		assert en.id not in self.preMap, Exception(en.name + ' has been assigned')
		emulator.add_node(en)
		en.add_var({
			'NET_CTL_ADDRESS': self.address,
			'NET_AGENT_ADDRESS': emulator.ipW + ':' + str(self.agentPort)
		})
		self.preMap[en.id] = emulator.idW

	def load_link(self, links_json: Dict):
		for name in links_json:
			src = self.name_to_node(name)
			for dest_json in links_json[name]:
				dest = self.name_to_node(dest_json['dest'])
				unit = dest_json['bw'][-4:]
				_bw = int(dest_json['bw'][:-4])
				self.__add_virtual_link(src, dest, _bw, unit)

	def load_link_user(self,taskId: int, links_json: Dict):
		for name in links_json:
			nodeName = str(taskId) + '_' + name
			src = self.name_to_node(nodeName)
			print(f"{nodeName}, {src}")
			for dest_json in links_json[name]:
				destName = str(taskId) + '_' + dest_json['dest']
				dest = self.name_to_node(destName)
				unit = dest_json['bw'][-4:]
				_bw = int(dest_json['bw'][:-4])
				print(f"{destName}, {dest}")
				self.__add_virtual_link(src, dest, _bw, unit)

	def name_to_node(self, name: str) -> Node:
		"""
		get node by name.
		"""
		if name in self.pNode:
			return self.pNode[name]
		elif name in self.eNode:
			return self.eNode[name]
		else:
			Exception('no such node called ' + name)

	def __add_virtual_link(self, n1: Node, n2: Node, bw: int, unit: str):
		"""
		parameters will be passed to Linux Traffic Control.
		n1-----bw----->>n2
		"""
		assert bw > 0, Exception('bw is not bigger than 0')
		assert unit in ['kbps', 'mbps'], Exception(
			unit + ' is not in ["kbps", "mbps"]')
		self.virtualLinkNumber += 1
		n1.link_to(n2.name, str(bw) + unit, n2.ip, n2.hostPort)

	# def save_link (self):
	# 	"""
	# 	save the tc settings as json file in json format.
	# 	the json content can be read by the following load_link ().
	# 	we use the name "links.json" in controller/dml_tool/*_structure_conf.py,
	# 	so please do not change it.
	# 	"""
	# 	links = {}
	# 	for pn in self.pNode.values ():
	# 		if not pn.tc:
	# 			continue
	# 		links [pn.name] = []
	# 		for dest in pn.tc:
	# 			links [pn.name].append ({"dest": dest, "bw": pn.tc [dest]})
	#
	# 	for e in self.emulator.values ():
	# 		for en in e.eNode.values ():
	# 			if not en.tc:
	# 				continue
	# 			links [en.name] = []
	# 			for dest in en.tc:
	# 				links [en.name].append ({"dest": dest, "bw": en.tc [dest]})
	#
	# 	filename = (os.path.join (self.dirName, 'links.json'))
	# 	with open (filename, 'w') as f:
	# 		f.writelines (json.dumps (links, indent=2))

	def save_node_info(self):
		"""
		save the node's information as json file.
		"""
		emulator = {}
		e_node = {}
		p_node = {}
		for e in self.emulator.values():
			emulator[e.nameW] = {'ip': e.ipW}
			for en in e.eNode.values():
				e_node[en.name] = {'ip': en.ip, 'port': str(en.hostPort), 'emulator': e.nameW}
		for pn in self.pNode.values():
			p_node[pn.name] = {'ip': pn.ip, 'port': str(pn.hostPort)}
		file_name = (os.path.join(self.dirName, 'node_info.json'))
		data = {'emulator': emulator, 'emulated_node': e_node, 'physical_node': p_node}
		with open(file_name, 'w') as f:
			f.writelines(json.dumps(data, indent=2))

	def save_yml(self):
		"""
		save the deployment of emulated nodes as yml files.
		"""
		for cs in self.emulator.values():
			cs.save_yml(self.dirName)
	
	def save_yml_user(self, taskId: int):
		"""
		save the deployment of emulated nodes as yml files.
		"""
		for cs in self.emulator.values():
			cs.save_yml_user(self.dirName, taskId)

	def __export_nfs(self):
		"""
		clear all exported path and then export the defined path through nfs.
		"""
		cmd = 'sudo exportfs -au'
		sp.Popen(cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait()
		for nfs in self.nfs.values():
			subnet = nfs.subnet
			path = nfs.path
			# export the path.
			cmd = 'sudo exportfs ' + subnet + ':' + path
			sp.Popen(cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait()
			# check result.
			cmd = 'sudo exportfs -v'
			p = sp.Popen(cmd, shell=True, stdout=sp.PIPE, stderr=sp.STDOUT)
			msg = p.communicate()[0].decode()
			assert path in msg and subnet in msg, Exception(
				'share ' + path + ' to ' + subnet + ' failed')

	def __send_emulator_info(self):
		"""
		send the ${ip:port} and emulator's name to emulators.
		this request can be received by worker/agent.py, route_emulator_info ().
		"""
		for e in self.emulator.values():
			print('send_emulator_info: send to ' + e.nameW)
			send_data('GET', '/emulator/info?address=' + self.address + '&name=' + e.nameW,
					  e.ipW, self.agentPort)
	
	def send_emulator_info(self):
		"""
		send the ${ip:port} and emulator's name to emulators.
		this request can be received by worker/agent.py, route_emulator_info ().
		"""
		for e in self.emulator.values():
			print('send_emulator_info: send to ' + e.nameW)
			send_data('GET', '/emulator/info?address=' + self.address + '&name=' + e.nameW,
					  e.ipW, self.agentPort)

	def __send_physical_nfs(self):
		"""
		send the nfs settings to physical nodes.
		this request can be received by worker/agent.py, route_physical_nfs ().
		"""
		tasks = [self.executor.submit(self.__send_physical_nfs_helper, pn)
				 for pn in self.pNode.values()]
		wait(tasks, return_when=ALL_COMPLETED)

	def __send_physical_nfs_helper(self, pn: PhysicalNode):
		data = {'ip': self.ip, 'nfs': pn.nfsMount}
		print('send_physical_nfs: send to ' + pn.name)
		res = send_data('POST', '/physical/nfs', pn.ip, self.agentPort,
						data={'data': json.dumps(data)})
		err = json.loads(res)
		if not err:
			print('physical node ' + pn.name + ' mount nfs succeed')
		else:
			print('physical node ' + pn.name + ' mount nfs failed, err:')
			print(err)

	def __send_physical_variable(self):
		"""
		send the variables to physical nodes.
		this request can be received by worker/agent.py, route_physical_variable ().
		"""
		for pn in self.pNode.values():
			print('send_physical_variable: send to ' + pn.name)
			send_data('POST', '/physical/variable', pn.ip, self.agentPort,
					  data={'data': json.dumps(pn.variable)})

	def __build_emulated_env(self, tag: str, path1: str, path2: str):
		"""
		send the Dockerfile and pip requirements.txt to emulators to build the execution environment.
		this request can be received by worker/agent.py, route_emulated_build ().
		@param tag: docker image name:version.
		@param path1: path of Dockerfile.
		@param path2: path of pip requirements.txt.
		@return:
		"""
		tasks = [self.executor.submit(self.__build_emulated_env_helper, e, tag, path1, path2)
				 for e in self.emulator.values()]
		wait(tasks, return_when=ALL_COMPLETED)

	def __build_emulated_env_helper(self, emulator: Emulator, tag: str, path1: str, path2: str):
		with open(path1, 'r') as f1, open(path2, 'r') as f2:
			print('build_emulated_env: send to ' + emulator.nameW)
			res = send_data('POST', '/emulated/build', emulator.ipW, self.agentPort,
							data={'tag': tag}, files={'Dockerfile': f1, 'dml_req': f2})
			if res == '1':
				print(emulator.nameW + ' build succeed')
			else:
				print(emulator.nameW + ' build failed')

	def __build_physical_env(self, path: str):
		"""
		send the dml_req.txt to physical nodes to build the execution environment.
		this request can be received by worker/agent.py, route_physical_build ().
		"""
		tasks = [self.executor.submit(self.__build_physical_env_helper, pn, path)
				 for pn in self.pNode.values()]
		wait(tasks, return_when=ALL_COMPLETED)

	def __build_physical_env_helper(self, pn: PhysicalNode, path: str):
		with open(path, 'r') as f:
			print('build_physical_env: send to ' + pn.name)
			res = send_data('POST', '/physical/build', pn.ip, self.agentPort,
							files={'dml_req': f})
			if res == '1':
				print(pn.name + ' build succeed')
			else:
				print(pn.name + ' build failed')

	def __send_tc(self):
		self.__set_emulated_tc_listener()
		if self.virtualLinkNumber > 0:
			# send the tc settings to emulators.
			self.__send_emulated_tc()
			# send the tc settings to physical nodes.
			self.__send_physical_tc()
		else:
			print('tc finish')

	def send_tc(self):
		self.__set_emulated_tc_listener()
		if self.virtualLinkNumber > 0:
			# send the tc settings to emulators.
			self.__send_emulated_tc()
			# send the tc settings to physical nodes.
			self.__send_physical_tc()
		else:
			print('tc finish')

	def __set_emulated_tc_listener(self):
		"""
		listen message from worker/agent.py, deploy_emulated_tc ().
		it will save the result of deploying emulated tc settings.
		"""

		@self.flask.route('/emulated/tc', methods=['POST'])
		def route_emulated_tc():
			data: Dict = json.loads(request.form['data'])
			for name, ret in data.items():
				if 'msg' in ret:
					print('emulated node ' + name + ' tc failed, err:')
					print(ret['msg'])
				elif 'number' in ret:
					print('emulated node ' + name + ' tc succeed')
					with self.lock:
						self.deployedCount += int(ret['number'])
						if self.deployedCount == self.virtualLinkNumber:
							print('tc finish')
			return ''

	def __send_emulated_tc(self):
		"""
		send the tc settings to emulators.
		this request can be received by worker/agent.py, route_emulated_tc ().
		"""
		for emulator in self.emulator.values():
			data = {}
			# collect tc settings of each emulated node in this emulator.
			for en in emulator.eNode.values():
				data[en.name] = {
					'NET_NODE_NIC': en.nic,
					'NET_NODE_TC': en.tc,
					'NET_NODE_TC_IP': en.tcIP,
					'NET_NODE_TC_PORT': en.tcPort
				}
			# the emulator will deploy all tc settings of its emulated nodes.
			print('send_emulated_tc: send to ' + emulator.nameW)
			send_data('POST', '/emulated/tc', emulator.ipW, self.agentPort,
					  data={'data': json.dumps(data)})

	def __send_physical_tc(self):
		"""
		send the tc settings to physical nodes.
		this request can be received by worker/agent.py, route_physical_tc ().
		"""
		for pn in self.pNode.values():
			if not pn.tc:
				print('physical node ' + pn.name + ' tc succeed')
				continue
			data = {
				'NET_NODE_NIC': pn.nic,
				'NET_NODE_TC': pn.tc,
				'NET_NODE_TC_IP': pn.tcIP,
				'NET_NODE_TC_PORT': pn.tcPort
			}
			print('physical_tc_update: send to ' + pn.name)
			res = send_data('POST', '/physical/tc', pn.ip, self.agentPort,
							data={'data': json.dumps(data)})
			if res == '':
				print('physical node ' + pn.name + ' tc succeed')
				with self.lock:
					self.deployedCount += len(pn.tc)
					if self.deployedCount == self.virtualLinkNumber:
						print('tc finish')
			else:
				print('physical node ' + pn.name + ' tc failed, err:')
				print(res)

	# def __send_ovs() 发送ovs信息

	def __launch_all_physical(self):
		"""
		send a launch message to physical nodes to launch the dml application.
		this request can be received by worker/agent.py, route_physical_launch ().
		"""
		tasks = [self.executor.submit(self.__launch_physical, p) for p in self.pNode.values()]
		wait(tasks, return_when=ALL_COMPLETED)

	def __launch_physical(self, pn: PhysicalNode):
		data = {'dir': pn.workingDir, 'cmd': pn.cmd}
		send_data('POST', '/physical/launch', pn.ip, self.agentPort, data={'data': json.dumps(data)})

	def launch_all_emulated(self):
		"""
		send the yml files to emulators to launch all emulated node and the dml application.
		this request can be received by worker/agent.py, route_emulated_launch ().
		"""
		tasks = []
		for s in self.emulator.values():
			if s.eNode:
				tasks.append(self.executor.submit(self.__launch_emulated, s, self.dirName))
		wait(tasks, return_when=ALL_COMPLETED)

	def __launch_emulated(self, emulator: Emulator, path: str):
		with open(os.path.join(path, emulator.nameW + '.yml'), 'r') as f:
			send_data('POST', '/emulated/launch', emulator.ipW, self.agentPort, files={'yml': f})

	def start(self, build_emulated_env: bool = False, build_physical_env: bool = False):
		"""
		start the test environment.
		"""

		# TODO ga搜索部署方案
		# self.__export_nfs()

		self.save_yml()
		self.save_node_info()
		self.manager.load_node_info()

		self.__send_emulator_info()
		self.__send_physical_nfs()
		self.__send_physical_variable()

		# TODO 做成选项，和role挂钩，role搞一个class
		# send the Dockerfile and dml_req.txt to emulators.
		# the path should be just a directory.
		# no need to call this function every time, unless you need to build a new docker image.
		if build_emulated_env:
			path_dockerfile = os.path.join(self.dirName, 'dml_app/Dockerfile')
			path_req = os.path.join(self.dirName, 'dml_app/dml_req.txt')
			self.__build_emulated_env('dml:v1.0', path_dockerfile, path_req)

		# TODO 做成选项，和role挂钩，role搞一个class
		# send the dml_req.txt to physical nodes.
		# the path should be just a directory.
		# no need to call this function every time, unless you need to install some new packages.
		if build_physical_env:
			path_req = os.path.join(self.dirName, 'dml_app/dml_req.txt')
			self.__build_physical_env(path_req)

		self.__send_tc()
		self.__launch_all_physical()
		self.launch_all_emulated()

		self.flask.run(host='0.0.0.0', port=self.port, threaded=True)


dirName = '/home/qianguo/controller/'
class TaskAnalyzer(object):
    def __init__(self, testbed: Testbed):
        self.testbed = testbed
        self.taskFileList: Dict[int, Dict] = {} 
        self.currtkId: int = 0

        self.__load_default_route()
        
    def __load_default_route(self):
        @self.testbed.flask.route('/taskRequestFile', methods=['POST'])
        def route_receive_request():
            """
            接收用户发送的任务文件，接收压缩包（包括links.json文件，dml_app,dml_tool,manager.py文件）
            task_file
                ├─ links.json
                ├─ manager.py
                ├─ dataset*
                    ├─ test_data
                    ├─ train_data
                ├─ dml_tool
                    ├─ dataset.json
                    ├─ structure.json
                    ├─ structure_conf.py
                    ├─ dataset_conf.py
                ├─ dml_app
                    ├─ nns
                    ├─ dml_req.txt
                    ├─ peer.py
                    ├─ Dockerfile
            """
            def analyse_file(taskId: int):
                """
                处理用户的文件，将压缩包解压并放到不同的文件夹中
                dml_app和dml_file需要和worker挂载，要不直接挂载一个大的文件夹，然后往里面更新算了（
                那挂载可以不动了，直接在下面为task创建对应的文件夹，文件路径改改
                worker_utils可以获得taskId，喜
                """
                zip_filename = f"{taskId}_taskFile.zip"
                zip_path = os.path.join(dirName, "task_file", zip_filename)
                
                current_directory = os.path.join(dirName, "task_file", str(taskId))
                os.makedirs(current_directory, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(current_directory)
                            
                # os.remove(zip_path)

                # 复制 links.json到task_links/{taskId}/links.json
                links_json_path = os.path.join(current_directory, 'links.json')
                target_links_json_path = os.path.join(dirName, "task_links", str(taskId))
                if not os.path.exists(target_links_json_path) :
                    os.makedirs(target_links_json_path)
                shutil.copy2(links_json_path, target_links_json_path)

                # 复制 manager.py到task_manager/{taskId}/ml_manager.py
                manager_path = os.path.join(current_directory, 'ml_manager.py')
                target_manager_path = os.path.join(dirName, "task_manager", str(taskId))
                if not os.path.exists(target_manager_path) :
                    os.makedirs(target_manager_path)
                shutil.copy2(manager_path, target_manager_path)
                
                # 创建 dml_tool 子目录
                target_dml_file_dir = os.path.join(dirName, "dml_tool", str(taskId))
                os.makedirs(target_dml_file_dir, exist_ok=True)
                
                # 复制 dml_file 文件夹到指定目录 /controller/dml_file/{taskId}/
                source_dml_file_dir = os.path.join(current_directory, 'dml_tool')
                if os.path.exists(source_dml_file_dir):
                    shutil.copytree(source_dml_file_dir, target_dml_file_dir, dirs_exist_ok=True)

                # 创建 dml_app 子目录
                target_dml_app_dir = os.path.join(dirName, "dml_app", str(taskId))
                os.makedirs(target_dml_app_dir, exist_ok=True)
                
                # 移动 dml_app 文件夹到指定目录 /controller/dml_app/{taskId}/
                source_dml_app_dir = os.path.join(current_directory, 'dml_app')
                if os.path.exists(source_dml_app_dir):
                    shutil.copytree(source_dml_app_dir, target_dml_app_dir, dirs_exist_ok=True)
                return
            if 'file' not in request.files:
                    return 'No file part', 400
                
            file = request.files['file']
                
            # 如果用户没有选择文件，浏览器可能会发送一个没有文件名的空文件
            if file.filename == '':
                return 'No selected file', 400
                
            if file:
                taskId = self.__next_task_id()
                filename = f"{taskId}_taskFile.zip"
                save_path = os.path.join(dirName,'task_file', filename)
                self.taskFileList[taskId] = {'links_file': filename}
                print(save_path)
                file.save(save_path)
                analyse_file(taskId)

                # 返回成功响应
                return 'File successfully uploaded.', 200
            
            return
        
        @self.testbed.flask.route('/startTask', methods=['GET'])
        def route_start_task():
            """
            用户信息已发送完毕，开始执行用户的任务，主要步骤有：
            1、处理用户文件
            2、分析任务请求
            3、将请求交给Scheduler处理并接收结果
            4、启动Edgetb（包括保存节点信息切割数据集生成对应的structure等步骤）
            5、返回实验结果
            """
            taskId = request.args.get('taskId')
            allocation = task_schedule(taskId)
            edgeTB_start(taskId,allocation)
            return ''

        def task_schedule(taskId: int):
            """
            丢给Scheduler处理，得到gl_run.py里的配置
            """
            allocation = self.testbed.scheduler.resource_schedule(taskId)
            for node, node_info in allocation.items(): 
                print(f"node name: {node}, node object: {node_info}")
            return allocation
            
        def edgeTB_start(taskId: int, allocation: Dict):
            """
            让edgetb启动相应的容器
            """
            # 挂载
            nfsApp = self.testbed.nfs['dml_app']
            nfsDataset = self.testbed.nfs['dataset']
                
            # 添加节点
            for node, node_info in allocation.items():
                emu = self.testbed.emulator[node_info['emulator']]
                en = self.testbed.add_emulated_node (node, '/home/qianguo/worker/dml_app/'+str(taskId),
                    ['python3', 'gl_peer.py'], 'dml:v1.0', cpu=node_info['cpu'], ram=node_info['ram'], unit='G', emulator=emu)
                en.mount_local_path ('./dml_file', '/home/qianguo/worker/dml_file')
                en.mount_nfs (nfsApp, '/home/qianguo/worker/dml_app')
                en.mount_nfs (nfsDataset, '/home/qianguo/worker/dataset')
                
            # 解析links
            links_json = read_json (os.path.join (dirName, "task_links", str(taskId),'links.json'))
            self.testbed.load_link_user (taskId, links_json)

            # 保存信息
            self.testbed.save_yml_user(taskId) # 保存yml文件到controller
            self.testbed.save_node_info() # 保存节点信息到testbed
            self.testbed.manager.load_node_info() # 保存节点信息到manager
            self.testbed.send_tc() # 将tc信息发送给worker，没有的添加，有的更新
            launch_all_emulated_user(self)
            
        def launch_all_emulated_user(self):
            tasks = []
            for s in self.testbed.emulator.values():
                if s.eNode:
                    tasks.append(self.testbed.executor.submit(launch_emulated_user, s, dirName))
            wait(tasks, return_when=ALL_COMPLETED)

        def launch_emulated_user(self, emulator: Emulator, taskId: int, path: str):
            with open(os.path.join(path, emulator.nameW + '_' + str(taskId) +'.yml'), 'r') as f:
                send_data('POST', '/emulated/launch', emulator.ipW, self.agentPort, files={'yml': f})

        def task_finish():
            """
            最终收尾工作
            """
    
    def __next_task_id(self):
        """
        获取下一个任务id
        """
        self.currtkId += 1
        return self.currtkId