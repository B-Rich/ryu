#Versione OpenState che sfrutta la tecnica del Fast Failover al posto dei Global States
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, HANDSHAKE_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ofproto_v1_3_parser
from ryu.lib.packet import packet
from ryu.topology import event
import logging
from sets import Set
import time
import f_t_parser_ff as f_t_parser
LOG = logging.getLogger('app.openstate.fault_tolerance_ff')

class OSFaultTolerance(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(OSFaultTolerance, self).__init__(*args, **kwargs)
        f_t_parser.generate_flow_entries_dict(GUI=True)

        # Associates dp_id to datapath object
        self.dp_dictionary=dict()
        self.ports_mac_dict=dict()

        # Detect nodes need group entries installation
        self.detect_nodes=Set([])
        for request in f_t_parser.requests:
            for y in range(len(f_t_parser.requests[request]['faults'])):
                self.detect_nodes.add(f_t_parser.requests[request]['faults'].items()[y][1]['detect_node'])

        # Primary path nodes match against "state=0" => they need to have a stateful stage 0
        self.stateful_nodes=Set([])
        for request in f_t_parser.requests:
            for y in range(len(f_t_parser.requests[request]['primary_path'])):
                self.stateful_nodes.add(f_t_parser.requests[request]['primary_path'][y])

        # Needed by fault_tolerance_rest
        self.f_t_parser = f_t_parser


    def save_datapath(self,dp_dictionary,dp_id,dp):
        dp_dictionary = dict(dp_dictionary.items() + [(dp_id, dp)])
        return dp_dictionary

    def install_probing(self, datapath):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser

        '''Redirect node rules'''
        if datapath.id==2:
            
            '''Packet duplication group entry'''
            buckets = []
            actions = [parser.OFPActionSetField(mpls_label=17),
                       parser.OFPActionOutput(port=3)]
            buckets.append(parser.OFPBucket(actions=actions))
            actions = [parser.OFPActionSetField(mpls_label=100),
                       parser.OFPActionOutput(port=1)]
            buckets.append(parser.OFPBucket(actions=actions))

            req = parser.OFPGroupMod(datapath=datapath, 
                                     type_=ofp.OFPGT_ALL, 
                                     group_id=0, 
                                     buckets=buckets)
            datapath.send_msg(req)

            '''Probing rule: packet duplicated in both primary and detour path'''
            match=parser.OFPMatch(in_port=2, state=100, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPExpActionSetState(state=17, table_id=0, hard_timeout=10, hard_rollback=100),
                       parser.OFPActionGroup(0)]
            self.add_flow(datapath=datapath, table_id=0, priority=10,
                    match=match, actions=actions)

            '''Match on probe packet: switch back to the primary path'''
            match=parser.OFPMatch(in_port=1, mpls_label=100, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPExpActionSetState(state=0, table_id=0)]
            self.add_flow(datapath=datapath, table_id=0, priority=10,
                    match=match, actions=actions)
            
            '''Failure: switch on the detour path and set probing timeout'''
            match=parser.OFPMatch(in_port=1, mpls_label=17, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPExpActionSetState(state=17, table_id=0, hard_timeout=10, hard_rollback=100),
                parser.OFPActionOutput(port=3)]
            self.add_flow(datapath=datapath, table_id=0, priority=10,
                    match=match, actions=actions, command=ofp.OFPFC_MODIFY)
        
        '''Detect node rules'''
        if datapath.id==3:
            '''Probe handler group entry'''
            buckets = []
            actions = [parser.OFPActionOutput(port=2)]
            buckets.append(parser.OFPBucket(watch_port=2, 
                                            actions=actions))
            actions = []
            buckets.append(parser.OFPBucket(watch_port=1, 
                                            actions=actions))
            req = parser.OFPGroupMod(datapath=datapath, 
                                     type_=ofp.OFPGT_FF, 
                                     group_id=0, 
                                     buckets=buckets)
            datapath.send_msg(req)

            '''Probe handler: if the link is up, the probe packet will be forwarded toward the next node, otherwise drop'''
            match=parser.OFPMatch(in_port=1, state=0, mpls_label=100, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPActionGroup(group_id=0)]
            self.add_flow(datapath=datapath, table_id=0, priority=100,
                    match=match, actions=actions)    
            '''It sends back a probe packet coming from the no more "unreachable node"'''
            match=parser.OFPMatch(in_port=2, state=0, mpls_label=100, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPActionOutput(1)]
            self.add_flow(datapath=datapath, table_id=0, priority=10,
                    match=match, actions=actions) 

        '''Unreachable node'''
        if datapath.id==4:
            
            '''probe handler: it sends back a probe message coming from the previous node'''
            match=parser.OFPMatch(in_port=1, state=0, mpls_label=100, eth_dst="00:00:00:00:00:06", eth_src="00:00:00:00:00:01", eth_type=0x8847)
            actions = [parser.OFPActionOutput(ofp.OFPP_IN_PORT)]
            self.add_flow(datapath=datapath, table_id=0, priority=100,
                    match=match, actions=actions) 


    def add_flow(self, datapath, table_id, priority, match, actions, command=0):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, table_id=table_id, command=command,
                                priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        self.ports_mac_dict[datapath.id]={}
        self.send_features_request(datapath)
        self.send_port_desc_stats_request(datapath)
        self.install_flows(datapath,datapath.id in self.stateful_nodes, datapath.id in self.detect_nodes)
        self.dp_dictionary = self.save_datapath(self.dp_dictionary,datapath.id,datapath)
        self.install_probing(datapath)

    def install_flows(self,datapath,stateful,has_group):
        print("Configuring flow table for switch %d" % datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # group entries installation
        if has_group:
            self.install_group_entries(datapath)

        if stateful:
            self.send_table_mod(datapath)
            self.send_key_lookup(datapath)
            self.send_key_update(datapath)

        # flow entries installation
        if datapath.id in f_t_parser.flow_entries_dict.keys():
            for flow_entry in f_t_parser.flow_entries_dict[datapath.id]:
                mod = parser.OFPFlowMod(
                    datapath=datapath, cookie=0, cookie_mask=0, table_id=flow_entry['table_id'],
                    command=ofproto.OFPFC_ADD, idle_timeout=0, hard_timeout=0,
                    priority=10, buffer_id=ofproto.OFP_NO_BUFFER,
                    out_port=ofproto.OFPP_ANY,
                    out_group=ofproto.OFPG_ANY,
                    flags=0, match=flow_entry['match'], instructions=flow_entry['inst'])
                datapath.send_msg(mod)     

    def send_table_mod(self, datapath):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        req = ofp_parser.OFPExpMsgConfigureStatefulTable(datapath=datapath, table_id=0, stateful=1)
        datapath.send_msg(req)

    def send_features_request(self, datapath):
        ofp_parser = datapath.ofproto_parser
        req = ofp_parser.OFPFeaturesRequest(datapath)
        datapath.send_msg(req)

    def send_key_lookup(self, datapath):
        ofp = datapath.ofproto
        key_lookup_extractor = datapath.ofproto_parser.OFPExpMsgKeyExtract(datapath=datapath, command=ofp.OFPSC_EXP_SET_L_EXTRACTOR, fields=[ofp.OXM_OF_ETH_SRC,ofp.OXM_OF_ETH_DST], table_id=0)
        datapath.send_msg(key_lookup_extractor)

    def send_key_update(self, datapath):
        ofp = datapath.ofproto
        key_update_extractor = datapath.ofproto_parser.OFPExpMsgKeyExtract(datapath=datapath, command=ofp.OFPSC_EXP_SET_U_EXTRACTOR, fields=[ofp.OXM_OF_ETH_SRC,ofp.OXM_OF_ETH_DST], table_id=0)
        datapath.send_msg(key_update_extractor)

    def set_link_down(self,node1,node2):
        ofproto = ofproto_v1_3
        parser = ofproto_v1_3_parser

        if(node1 > node2):
            node1,node2 = node2,node1

        hw_addr1 = self.ports_mac_dict[self.dp_dictionary[node1].id][f_t_parser.mn_topo_ports['s'+str(node1)]['s'+str(node2)]]
        hw_addr2 = self.ports_mac_dict[self.dp_dictionary[node2].id][f_t_parser.mn_topo_ports['s'+str(node2)]['s'+str(node1)]]
        config = 1
        mask = (ofproto.OFPPC_PORT_DOWN)
        advertise = (ofproto.OFPPF_10MB_HD | ofproto.OFPPF_100MB_FD |
                     ofproto.OFPPF_1GB_FD | ofproto.OFPPF_COPPER |
                     ofproto.OFPPF_AUTONEG | ofproto.OFPPF_PAUSE |
                     ofproto.OFPPF_PAUSE_ASYM)
        req1 = parser.OFPPortMod(self.dp_dictionary[node1], f_t_parser.mn_topo_ports['s'+str(node1)]['s'+str(node2)], hw_addr1, config,
                                    mask, advertise)
        self.dp_dictionary[node1].send_msg(req1)
        req2 = parser.OFPPortMod(self.dp_dictionary[node2], f_t_parser.mn_topo_ports['s'+str(node2)]['s'+str(node1)], hw_addr2, config,
                                    mask, advertise)
        self.dp_dictionary[node2].send_msg(req2)

    def set_link_up(self,node1,node2):
        ofproto = ofproto_v1_3
        parser = ofproto_v1_3_parser

        if(node1 > node2):
            node1,node2 = node2,node1

        hw_addr1 = self.ports_mac_dict[self.dp_dictionary[node1].id][f_t_parser.mn_topo_ports['s'+str(node1)]['s'+str(node2)]]
        hw_addr2 = self.ports_mac_dict[self.dp_dictionary[node2].id][f_t_parser.mn_topo_ports['s'+str(node2)]['s'+str(node1)]]
        config = 0
        mask = (ofproto.OFPPC_PORT_DOWN)
        advertise = (ofproto.OFPPF_10MB_HD | ofproto.OFPPF_100MB_FD |
                     ofproto.OFPPF_1GB_FD | ofproto.OFPPF_COPPER |
                     ofproto.OFPPF_AUTONEG | ofproto.OFPPF_PAUSE |
                     ofproto.OFPPF_PAUSE_ASYM)
        req1 = parser.OFPPortMod(self.dp_dictionary[node1], f_t_parser.mn_topo_ports['s'+str(node1)]['s'+str(node2)], hw_addr1, config,
                                    mask, advertise)
        self.dp_dictionary[node1].send_msg(req1)
        req2 = parser.OFPPortMod(self.dp_dictionary[node2], f_t_parser.mn_topo_ports['s'+str(node2)]['s'+str(node1)], hw_addr2, config,
                                    mask, advertise)
        self.dp_dictionary[node2].send_msg(req2)

        # STATE update in Redirect only nodes of all requests involved in fault
        '''for req in f_t_parser.faults[(node1,node2)]['requests']:
            redirect_node = f_t_parser.faults[(node1,node2)]['requests'][req]['redirect_node']
            self.add_state_entry(self.dp_dictionary[redirect_node],req[0],req[1])'''

    def add_state_entry(self, datapath, mac_src, mac_dst):
        ofproto = datapath.ofproto
        state = datapath.ofproto_parser.OFPExpMsgSetFlowState(
            datapath, state=0, state_mask=0xffffffff, keys=[0,0,0,0,0,mac_src,0,0,0,0,0,mac_dst], table_id=0)
        datapath.send_msg(state)

    def install_group_entries(self,datapath):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        for group_entry in f_t_parser.group_entries_dict[datapath.id]:
            buckets = f_t_parser.group_entries_dict[datapath.id][group_entry]
            req = ofp_parser.OFPGroupMod(datapath, ofp.OFPGC_ADD,ofp.OFPGT_FF, group_entry, buckets)
            datapath.send_msg(req)
            
    def send_port_desc_stats_request(self, datapath):
        ofp_parser = datapath.ofproto_parser

        req = ofp_parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        for p in ev.msg.body:
            self.ports_mac_dict[ev.msg.datapath.id][p.port_no]=p.hw_addr