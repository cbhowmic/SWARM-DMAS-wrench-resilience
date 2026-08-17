#!/usr/bin/env python3
import glob
import heapq
import json
import os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "fabric_data")
OUT = os.path.join(HERE, "generated", "fabric_network.json")
CORE = ['STAR','NEWY','WASH','ATLA','DALL','LOSA','SALT','KANS','SEAT']
COLOCATE = [('EDC','NCSA',100,0.05)]
os.makedirs(os.path.dirname(OUT), exist_ok=True)

def load_mx():
    m = {}
    for p in sorted(glob.glob(os.path.join(DATA_DIR, 'matrix_*.json'))):
        with open(p) as f:
            m.update(json.load(f))
    return m


def simple_paths(adj, s, t, maxnodes):
    out, stack = [], [(s,[s])]
    while stack:
        u,p = stack.pop()
        if u == t:
            out.append(p); continue
        if len(p) >= maxnodes:
            continue
        for v in adj[u]:
            if v not in p:
                stack.append((v,p+[v]))
    return out


def main():
    with open(os.path.join(DATA_DIR,'us_topology.json')) as f:
        topo = json.load(f)
    smeta = topo['sites']
    mx = load_mx()
    nominal = {frozenset((l['a'],l['b'])): dict(l) for l in topo['links']}

    lat, srcflag, ignored = {}, {}, []
    measured_pairs = {}
    for k,r in mx.items():
        if r.get('one_way_ms') is None:
            continue
        a,b = k.split('__')
        measured_pairs[(a,b)] = (r['one_way_ms'],r.get('sites_on_path'))

    for l in topo['links']:
        a,b = l['a'],l['b']
        e = frozenset((a,b))
        r = mx.get(f'{a}__{b}') or mx.get(f'{b}__{a}')
        if r and r.get('sites_on_path') == 2 and r.get('one_way_ms'):
            lat[e] = r['one_way_ms']; srcflag[e] = 'measured'
        elif r and r.get('sites_on_path') and r['sites_on_path'] > 2:
            ignored.append({
                'source': a, 'destination': b,
                'nominal_bandwidth_gbps': l['gbps'],
                'observed_one_way_ms': r['one_way_ms'],
                'observed_sites_on_path': r['sites_on_path'],
                'reason': 'Physical FABRIC link is not used directly by FABNetv4; traffic is routed through other sites.'
            })
        else:
            lat[e] = round(l['floor_ms'] * 1.45, 4); srcflag[e] = 'estimated'

    for a,b,g,lt in COLOCATE:
        if a in smeta:
            e = frozenset((a,b))
            lat[e] = lt; srcflag[e] = 'colocated'
            nominal[e] = {'a':a,'b':b,'gbps':g,'layer':'synthetic-local','km':0.0,'floor_ms':0.0}

    adj = {n:set() for n in smeta}
    for e in lat:
        a,b = tuple(e); adj[a].add(b); adj[b].add(a)

    def elat(u,v): return lat[frozenset((u,v))]

    def reconstruct(a,b):
        rab = measured_pairs.get((a,b)); rba = measured_pairs.get((b,a))
        if not rab and not rba:
            return None
        meas = (rab or rba)[0]
        for sit in sorted({r[1] for r in (rab,rba) if r and r[1]}):
            cands = [p for p in simple_paths(adj,a,b,sit) if len(p)==sit]
            if cands:
                return min(cands,key=lambda p: abs(sum(elat(p[i],p[i+1]) for i in range(len(p)-1))-meas))
        return None

    def minhop(a,b):
        dist={a:(0,0.0)}; prev={}; pq=[(0,0.0,a)]
        while pq:
            h,d,u=heapq.heappop(pq)
            if (h,d) > dist.get(u,(10**9,float('inf'))): continue
            for v in adj[u]:
                nd=(h+1,d+elat(u,v))
                if nd < dist.get(v,(10**9,float('inf'))):
                    dist[v]=nd; prev[v]=u; heapq.heappush(pq,(nd[0],nd[1],v))
        if b not in prev and b != a: return None
        seq=[b]
        while seq[-1] != a: seq.append(prev[seq[-1]])
        return seq[::-1]

    def core_seq(p,q): return [p] if p==q else (reconstruct(p,q) or minhop(p,q))

    def attach(site):
        if site in CORE: return [[site]]
        res=[]; stack=[[site]]
        while stack:
            seq=stack.pop(); u=seq[-1]
            for v in adj[u]:
                if v in seq: continue
                if v in CORE: res.append(seq+[v])
                elif len(seq)<4: stack.append(seq+[v])
        return res or [[site]]

    def seq_lat(seq): return sum(elat(seq[i],seq[i+1]) for i in range(len(seq)-1))

    def full_path(a,b):
        if frozenset((a,b)) in lat: return [a,b]
        r=reconstruct(a,b)
        if r: return r
        best=None
        for ua in attach(a):
            for ub in attach(b):
                ca,cb=ua[-1],ub[-1]
                mid=core_seq(ca,cb)
                if mid is None: continue
                seq=ua[:-1]+mid+ub[-2::-1]
                if len(set(seq)) != len(seq): continue
                L=seq_lat(seq)
                if best is None or L < best[1]: best=(seq,L)
        return best[0] if best else None

    sites=[]
    for name in sorted(smeta):
        meta=smeta[name]
        sites.append({
            'name': name,
            'address': meta.get('addr'),
            'location': {'latitude': meta.get('lat'), 'longitude': meta.get('lon')},
            'network': {
                'role': 'core' if name in CORE else 'edge',
                'degree_in_modeled_fabnet_graph': len(adj[name])
            },
            'storage_systems': [],
            'filesystems': [],
            'clusters': []
        })

    links=[]
    for e in sorted(lat,key=lambda e: sorted(e)):
        a,b=sorted(e); n=nominal[e]
        links.append({
            'name': f'{a}__{b}',
            'source': a,
            'destination': b,
            'bandwidth_gbps': n['gbps'],
            'latency_ms': lat[e],
            'latency_source': srcflag[e],
            'physical_layer': n.get('layer'),
            'great_circle_distance_km': n.get('km'),
            'theoretical_fiber_floor_ms': n.get('floor_ms')
        })

    nodes=sorted(smeta)
    routes=[]; unreachable=[]
    for i,a in enumerate(nodes):
        for b in nodes[i+1:]:
            seq=full_path(a,b)
            if not seq:
                unreachable.append([a,b]); continue
            hops=[]
            for k in range(len(seq)-1):
                x,y=sorted((seq[k],seq[k+1]))
                hops.append(f'{x}__{y}')
            routes.append({
                'source': a,
                'destination': b,
                'site_path': seq,
                'links': hops,
                'modeled_one_way_latency_ms': round(seq_lat(seq),4),
                'symmetric': True
            })

    doc={
        'schema_version':'1.0',
        'name':'FABRIC-US-network',
        'description':'Intermediate SWARM network description derived from the NRTWsim FABRIC platform investigation. Compute clusters, site storage, and filesystems are intentionally left empty for later synthetic specification.',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'provenance': {
            'source_repository':'NRTWsim',
            'source_directory':'investigations/platform-files',
            'site_and_physical_link_source':'us_topology.json',
            'latency_and_hop_source':['matrix_fab-backbone.json','matrix_fab-leaves.json'],
            'routing_method':'Same measured-path reconstruction and leaf composition logic as 05_emit_platform.py',
            'bandwidth_semantics':'Nominal FABRIC link bandwidth from list_links-derived topology; not measured throughput.',
            'latency_semantics':'One-way latency is min RTT / 2 when measured; a small number of uplinks are estimated; EDC-NCSA is modeled as colocated.',
            'compute_metadata_policy':'FABRIC cores_capacity/cores_available are intentionally excluded because SWARM compute clusters will be synthetic.'
        },
        'sites':sites,
        'links':links,
        'routes':routes,
        'excluded_physical_links':ignored,
        'validation': {
            'site_count':len(sites),
            'modeled_direct_link_count':len(links),
            'route_count':len(routes),
            'expected_all_pairs_route_count':len(nodes)*(len(nodes)-1)//2,
            'unreachable_pairs':unreachable,
            'latency_sources':{
                'measured':sum(1 for e in lat if srcflag[e]=='measured'),
                'estimated':sum(1 for e in lat if srcflag[e]=='estimated'),
                'colocated':sum(1 for e in lat if srcflag[e]=='colocated')
            }
        }
    }
    with open(OUT,'w') as f: json.dump(doc,f,indent=2)
    print(json.dumps(doc['validation'],indent=2))
    print('excluded', len(ignored), ignored)
    print('wrote', OUT)

if __name__=='__main__': main()
