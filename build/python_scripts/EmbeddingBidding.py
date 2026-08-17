import sys
import json
import time
import math
import numpy as np

JOB_TYPES = ["HPC", "AI", "HYBRID", "STORAGE"]
SITES = ["NERSC", "ALCF", "OLCF"]


def one_hot(value, vocab):
    v = np.zeros(len(vocab) + 1, dtype=np.float32)  # +1 for OTHER
    if value in vocab:
        v[vocab.index(value)] = 1.0
    else:
        v[-1] = 1.0
    return v

def l2_normalize(x, eps=1e-12):
    x = np.asarray(x, dtype=np.float32)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    n = float(np.linalg.norm(x))
    if not math.isfinite(n) or n < eps:
        return np.zeros_like(x, dtype=np.float32)
    return x / n

def fnum(x, default=0.0):
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)

def finite_or_inf(x):
    try:
        v = float(x)
    except Exception:
        return float("inf")
    if not math.isfinite(v):
        return float("inf")
    return v


def embed_job(job):
    nodes = fnum(job.get("num_nodes"), 0.0)
    wall = fnum(job.get("walltime"), 0.0)
    mem = fnum(job.get("requested_memory_gb"), 0.0)
    storage = fnum(job.get("requested_storage_gb"), 0.0)
    gpu = 1.0 if bool(job.get("needs_gpu", False)) else 0.0

    job_type = (job.get("job_type") or "OTHER")
    job_site = (job.get("hpc_site") or "OTHER")

    NODES_MAX = 2048.0
    WALL_MAX = 10080.0 * 60.0   # 7 days in seconds
    MEM_MAX = 1e6
    STORAGE_MAX = 500.0

    def log01(x, cap):
        x = max(0.0, float(x))
        x = min(x, cap)
        return math.log1p(x) / math.log1p(cap)

    x_num = np.array([
        log01(nodes, NODES_MAX),
        log01(wall, WALL_MAX),
        log01(mem, MEM_MAX),
        log01(storage, STORAGE_MAX),
        gpu,
    ], dtype=np.float32)

    x_type = one_hot(job_type, JOB_TYPES)
    x_site = one_hot(job_site, SITES)

    w_num  = 1.0
    w_type = 0.7
    w_site = 0.5

    x = np.concatenate([w_num * x_num, w_type * x_type, w_site * x_site], axis=0)
    return l2_normalize(x)


def embed_system(sysdesc, job_site_hint=None):
    sys_nodes = fnum(sysdesc.get("num_nodes"), 1.0)
    sys_has_gpu = 1.0 if bool(sysdesc.get("has_gpu", False)) else 0.0
    sys_type = (sysdesc.get("type") or "OTHER")
    sys_site = sysdesc.get("site") or (job_site_hint or "OTHER")
    sys_speed = fnum(sysdesc.get("node_speed"), 1.0)
    sys_mem_per_node = fnum(sysdesc.get("memory_amount_in_gb"), 0.0)

    sys_storage_cap = finite_or_inf(sysdesc.get("storage_amount_in_gb", float("inf")))
    if sys_storage_cap == float("inf"):
        sys_storage_cap = 1e9 

    NODES_MAX = 10624.0         # Aurora has the most nodes
    SPEED_MAX = 312e12          # Aurora speed in FLOPS
    MEM_MAX = 9472*12000        # Total memory in GB for Frontier
    STORAGE_MAX = 700e6         # 700 PB for Summit

    def log01(x, cap):
        x = max(0.0, float(x))
        x = min(x, cap)
        return math.log1p(x) / math.log1p(cap)

    x_num = np.array([
        log01(sys_nodes, NODES_MAX),
        log01(sys_speed, SPEED_MAX),
        log01(sys_mem_per_node, MEM_MAX),
        log01(sys_storage_cap, STORAGE_MAX),
        sys_has_gpu,
    ], dtype=np.float32)

    x_type = one_hot(sys_type, JOB_TYPES)
    x_site = one_hot(sys_site, SITES)

    w_num  = 1.0
    w_type = 0.7
    w_site = 0.5

    x = np.concatenate([w_num * x_num, w_type * x_type, w_site * x_site], axis=0)
    return l2_normalize(x)  


# Function that computes bid based on embeddings
def compute_bid(job, sysdesc, status, current_simulated_time=0.0):

    # Feasibility 
    nodes_req    = fnum(job.get("num_nodes"), 0.0)
    req_gpu      = bool(job.get("needs_gpu", False))
    req_mem      = fnum(job.get("requested_memory_gb"), 0.0)
    req_storage  = fnum(job.get("requested_storage_gb"), 0.0)
    req_walltime = fnum(job.get("walltime"), 0.0)
    job_submission_time = fnum(job.get("submission_time"), 0.0)

    sys_nodes    = fnum(sysdesc.get("num_nodes"), 0.0)
    sys_has_gpu  = bool(sysdesc.get("has_gpu", False))
    sys_speed = fnum(sysdesc.get("node_speed"), 1.0)
    sys_mem_per_node = fnum(sysdesc.get("memory_amount_in_gb"), 0.0)
    sys_total_mem = sys_mem_per_node * sys_nodes

    sys_total_storage = finite_or_inf(sysdesc.get("storage_amount_in_gb", float("inf")))

    if nodes_req > sys_nodes:
        return 0.0
    if req_gpu and not sys_has_gpu:
        return 0.0
    if req_mem > sys_total_mem:
        return 0.0
    if req_storage > sys_total_storage:
        return 0.0
    if req_walltime <= 0:
        return 0.0

    # Embeddings for job and site
    e_job = embed_job(job)
    e_sys = embed_system(sysdesc, job_site_hint=job.get("hpc_site"))

    # Relative node availability 
    sys_avail_nodes = fnum(status.get("current_num_available_nodes"), 0.0)
    headroom = sys_avail_nodes / max(1.0, nodes_req)
    headroom_feat = headroom / (1.0 + headroom)
    
    # Estimated slowdown using system status
    est_start_time = status.get("current_job_start_time_estimate")
    wait_time = max(0, est_start_time - job_submission_time) 
    sys_speed = fnum(sysdesc.get("node_speed"), 1.0)
    BASE_SPEED= 1500000000000.0
    sys_perf = max(1e-3, sys_speed / max(1e-9, BASE_SPEED))
    pred_exec_time = req_walltime / max(1e-9, sys_perf)
    slowdown = (wait_time + pred_exec_time) / max(1.0, pred_exec_time)
    alpha = 0.1
    slowdown_feat = math.exp(-alpha * slowdown)

    dynamic_bid = 0.5 * headroom_feat + 0.5 * slowdown_feat

    raw = float(np.dot(e_job, e_sys)) 
    static_bid = max(0.0, raw)
    bid = 0.6 * static_bid + 0.4 * dynamic_bid
    if not math.isfinite(bid):
        bid = 0.0

    return bid


def main():
    input_data = sys.stdin.read()
    data = json.loads(input_data)

    job_description = data["job_description"]
    system_description = data["hpc_system_description"]
    system_status = data["hpc_system_status"]
        
    # Start timing
    start_time = time.perf_counter()

    # Compute bids
    try:
        bid = compute_bid(job_description, system_description, system_status)
    except Exception as e:
        print(json.dumps({"error": str(e)}))

    # End timing
    end_time = time.perf_counter()
    elapsed_time = end_time - start_time

    # Do not modify after here
    result = {
        "bid": bid,
        "bid_generation_time_seconds": round(elapsed_time, 6)
    }

    print(json.dumps(result))


if __name__ == "__main__":
    main()