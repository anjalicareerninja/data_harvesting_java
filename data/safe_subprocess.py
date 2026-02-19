import json
import os
import signal
import subprocess
import threading
import time
import psutil
from typing import List
from log import setup_logger
from env import SANDBOX_UID, SANDBOX_GID, ENV

try:
    import fcntl
except ImportError:
    fcntl = None

# 初始化日志
logger = setup_logger()

MAX_BYTES_PER_READ = 1024
SLEEP_BETWEEN_READS = 0.1
try:
    SC_CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
except (AttributeError, OSError):
    SC_CLK_TCK = 100
CPU_COUNT = os.cpu_count() or 1


def set_nonblocking(reader):
    if fcntl is None:
        return
    fd = reader.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

def get_system_cpu():
    if os.name != "posix" or not os.path.exists("/proc/stat"):
        return 0
    try:
        with open("/proc/stat") as proc_stat:
            cpu_total = proc_stat.readline().strip().split()[1:]
            cpu_total = [int(i) for i in cpu_total]
            system_cpu = sum(cpu_total)
    except Exception as _:
        system_cpu = 0
    return system_cpu


def get_process_cpu_mem(pid):
    try:
        parent = psutil.Process(pid)
        if os.name != "posix":
            # Windows: use psutil memory only
            mem = parent.memory_info()
            return 0, getattr(mem, "rss", mem[0]) // 1024
        descendants = parent.children(recursive=True)
        all_processes = [parent] + descendants
        process_cpu = 0
        process_peak_memory = 0
        for process in all_processes:
            with open(f"/proc/{process.pid}/stat") as pid_stat:
                vals = pid_stat.read().split()
                process_cpu += sum(map(float, vals[13:17]))
            with open(f"/proc/{process.pid}/status") as pid_status:
                vm_peak_line = [l for l in pid_status if l.startswith("VmPeak:")]
                if len(vm_peak_line) != 0:
                    vm_peak_line = vm_peak_line[0]
                    vm_peak_line = vm_peak_line.split(":")[-1].strip()
                    if vm_peak_line.endswith("kB"):
                        process_peak_memory += int(vm_peak_line.split()[0])
                    elif vm_peak_line.endswith("mB"):
                        process_peak_memory += int(vm_peak_line.split()[0]) * 1024
                    elif vm_peak_line.endswith("gB"):
                        process_peak_memory += int(vm_peak_line.split()[0]) * 1024 * 1024
                    else:
                        process_peak_memory += int(vm_peak_line.split()[0])
        return process_cpu, process_peak_memory
    except Exception as _:
        return 0, 0

def run(
    args: List[str],
    timeout_seconds: int = 15,
    max_output_size: int = 2048,
    env=None,
    shell=False,
    cwd=None
) -> dict:
    start_time = time.time()
    logger.info(f"执行命令: {args}, 工作目录: {cwd}")
    if env is None:
        env = ENV.copy()
    popen_kw = dict(
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=MAX_BYTES_PER_READ,
        shell=False,
        cwd=cwd,
    )
    if SANDBOX_UID is not None:
        popen_kw["user"] = SANDBOX_UID
        popen_kw["group"] = SANDBOX_GID
        popen_kw["start_new_session"] = True
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(args, **popen_kw)

    system_cpu_start = get_system_cpu()
    process_cpu = 0
    process_peak_memory = 0

    process_group_id = os.getpgid(p.pid) if hasattr(os, "getpgid") else None

    # On Windows pipes are blocking; reading one while child writes to the other deadlocks.
    # Use threads to read both streams, or non-blocking I/O on Unix.
    stdout_saved_bytes = []
    stderr_saved_bytes = []
    stdout_bytes_read = [0]  # use list so closure can mutate
    stderr_bytes_read = [0]

    def read_stream(stream, out_list, size_holder):
        try:
            while size_holder[0] < max_output_size:
                chunk = stream.read(MAX_BYTES_PER_READ)
                if not chunk:
                    break
                out_list.append(chunk)
                size_holder[0] += len(chunk)
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    if os.name == "nt":
        t_out = threading.Thread(target=read_stream, args=(p.stdout, stdout_saved_bytes, stdout_bytes_read))
        t_err = threading.Thread(target=read_stream, args=(p.stderr, stderr_saved_bytes, stderr_bytes_read))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()
        max_iterations = timeout_seconds * 10
        for _ in range(max_iterations):
            try:
                cur_cpu, cur_mem = get_process_cpu_mem(p.pid)
                process_peak_memory = max(process_peak_memory, cur_mem or 0)
            except Exception:
                pass
            exit_code = p.poll()
            if exit_code is not None:
                break
            time.sleep(SLEEP_BETWEEN_READS)
        t_out.join(timeout=0.5)
        t_err.join(timeout=0.5)
    else:
        set_nonblocking(p.stdout)
        set_nonblocking(p.stderr)
        max_iterations = timeout_seconds * 10
        for _ in range(max_iterations):
            try:
                cur_cpu, cur_mem = get_process_cpu_mem(p.pid)
                process_peak_memory = max(process_peak_memory, cur_mem or 0)
            except Exception:
                pass
            this_stdout_read = p.stdout.read(MAX_BYTES_PER_READ)
            this_stderr_read = p.stderr.read(MAX_BYTES_PER_READ)
            if this_stdout_read and stdout_bytes_read[0] < max_output_size:
                stdout_saved_bytes.append(this_stdout_read)
                stdout_bytes_read[0] += len(this_stdout_read)
            if this_stderr_read and stderr_bytes_read[0] < max_output_size:
                stderr_saved_bytes.append(this_stderr_read)
                stderr_bytes_read[0] += len(this_stderr_read)
            exit_code = p.poll()
            if exit_code is not None:
                break
            time.sleep(SLEEP_BETWEEN_READS)

    try:
        if process_group_id is not None:
            os.killpg(process_group_id, signal.SIGKILL)
        else:
            p.kill()
    except (ProcessLookupError, OSError):
        pass

    timeout = exit_code is None
    exit_code = exit_code if exit_code is not None else -1
    stdout = b"".join(stdout_saved_bytes).decode("utf-8", errors="ignore")
    stderr = b"".join(stderr_saved_bytes).decode("utf-8", errors="ignore")
    system_cpu_end = get_system_cpu()
    if system_cpu_start != system_cpu_end:
        process_cpu_util = (
            process_cpu / (system_cpu_end - system_cpu_start) * 100 * CPU_COUNT
        )
    else:
        process_cpu_util = 0
    end_time = time.time()
    process_exec_time = end_time - start_time
    result = {
        "cmd": args, # 最终执行的命令
        "timeout": timeout,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "process_cpu_util": round(process_cpu_util, 2), # 进程占用的平均cpu利用率, 单位: %
        "process_cpu_time": round(process_cpu / SC_CLK_TCK, 2), # 进程占用的总cpu时间, 单位: s
        "process_exec_time": round(process_exec_time, 2), # 进程执行时间, 单位: s
        "process_peak_memory": process_peak_memory, # 进程执行过程中占用的最大内存, 单位: kB
    }
    return result