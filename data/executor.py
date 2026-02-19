#!/usr/bin/env python3

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from exec_outcome import ExecOutcome

# 使用自定义的安全子进程模块替代标准库, 确保安全性和超时控制
import safe_subprocess as subprocess  # 使用自定义的 subprocess 模块
from log import setup_logger

# 初始化日志
logger = setup_logger()


class LanguageExecutor:
    def __init__(self):
        pass

    def execute(self, language_config, timeout):
        code_path = language_config["code_path"]
        src_uid = language_config["src_uid"]
        code_dir = os.path.dirname(code_path)
        # 设置超时时间
        timeout = timeout + language_config.get("timelimit_factor", 0)
        logger.info(
            f"src_uid: {src_uid}, 执行语言: {language_config['lang']}, 文件路径: {code_path}, 临时目录: {code_dir}, 超时时间: {timeout}"
        )

        # 检查是否需要特殊处理
        if "handler" in language_config:
            return self._execute_special(
                language_config, src_uid, code_dir, timeout
            )

        # 通用执行流程：编译 + 运行
        return self._execute_generic(
            language_config, src_uid, code_dir, timeout, code_dir
        )

    def _update_run_result(self, language_config, run_result, final_result):
        if run_result["timeout"]:
            final_result["outcome"] = ExecOutcome.TIME_LIMIT_EXCEEDED.name
        elif run_result["exit_code"] != 0:
            final_result["outcome"] = ExecOutcome.RUNTIME_ERROR.name
        elif self._check_language_specific_errors(language_config, run_result):
            final_result["outcome"] = ExecOutcome.WRONG_ANSWER.name
        else:
            final_result["outcome"] = ExecOutcome.PASSED.name

    def _execute_generic(self, language_config, src_uid, code_dir, timeout, cwd):
        """通用执行流程：先编译（如果需要），后运行"""

        result = {"language": language_config["lang"], "path": language_config["code_path"]}

        # 获取配置
        compile_cmd = language_config.get("compile_cmd", "")
        compile_flags = language_config.get("compile_flags", [])
        execute_cmd = language_config.get("execute_cmd", "")
        execute_flags = language_config.get("execute_flags", [])

        # 准备变量替换
        file_name = os.path.basename(language_config["code_path"])
        module_name = os.path.splitext(file_name)[0]
        executable_name = language_config.get("executable_name", module_name)

        substitutions = {
            "file_path": language_config["code_path"],
            "file_name_template": file_name,
            "module_name": module_name,
            "executable_name": executable_name,
            "code_dir": code_dir
        }

        # 第一步：编译（如果需要）
        if compile_cmd and compile_cmd.strip():
            # 处理编译命令和参数
            processed_compile_flags = []
            for flag in compile_flags:
                if isinstance(flag, str):
                    flag = flag.format(**substitutions)
                processed_compile_flags.append(flag)

            compile_command = [compile_cmd] + processed_compile_flags

            compile_result = subprocess.run(compile_command, timeout_seconds=timeout, cwd=cwd)
            logger.info(
                f"src_uid: {src_uid}, {language_config['lang']} 编译结果, 标准输出: {compile_result['stdout']}, 错误输出: {compile_result['stderr']}, 返回码: {compile_result['exit_code']}, process_cpu_util: {compile_result['process_cpu_util']} %, process_cpu_time: {compile_result['process_cpu_time']} s, process_peak_memory: {compile_result['process_peak_memory']} kB"
            )
            result.update(compile_result)
            if compile_result["timeout"]:
                result["outcome"] = ExecOutcome.TIME_LIMIT_EXCEEDED.name
                return result
            elif compile_result["exit_code"] != 0:
                result["outcome"] = ExecOutcome.COMPILATION_ERROR.name
                return result

        # 第二步：执行（如果有执行命令）
        if execute_cmd and execute_cmd.strip():
            logger.info(f"src_uid: {src_uid}, 开始执行 {language_config['lang']} 代码")

            # 处理执行命令和参数
            processed_execute_cmd = execute_cmd.format(**substitutions)
            processed_execute_flags = []
            for flag in execute_flags:
                if isinstance(flag, str):
                    flag = flag.format(**substitutions)
                processed_execute_flags.append(flag)

            command = [processed_execute_cmd] + processed_execute_flags

            run_result = subprocess.run(command, timeout_seconds=timeout, cwd=cwd)
            logger.info(
                f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 标准输出: {run_result['stdout']}, 错误输出: {run_result['stderr']}, 返回码: {run_result['exit_code']}, process_cpu_util: {run_result['process_cpu_util']} %, process_cpu_time: {run_result['process_cpu_time']} s, process_peak_memory: {run_result['process_peak_memory']} kB"
            )
            result.update(run_result)

            self._update_run_result(language_config, run_result, result)
        else:
            # 没有执行命令，只是编译检查
            result["outcome"] = ExecOutcome.PASSED.name

        return result

    def _execute_special(self, language_config, src_uid, code_dir, timeout):
        """执行特殊处理的语言"""
        handler = language_config["handler"]

        if handler == "dotnet_handler":
            return self._handle_dotnet(
                language_config, src_uid, code_dir, timeout
            )
        elif handler == "erlang_handler":
            return self._handle_erlang(
                language_config, src_uid, code_dir, timeout
            )
        # 只在代码处理阶段特殊处理，执行阶段不特殊处理
        elif handler == "rust_handler":
            return self._execute_generic(
                language_config, src_uid, code_dir, timeout, code_dir
            )
        elif handler == "go_handler":
            return self._handle_go(
                language_config, src_uid, code_dir, timeout
            )
        elif handler == "java_handler":
            return self._handle_java(
                language_config, src_uid, code_dir, timeout
            )
        else:
            raise ValueError(f"不支持的特殊处理器: {handler}")

    def _check_language_specific_errors(self, language_config, run_result):
        """检查语言特定的错误"""
        # 从配置获取错误检查规则
        error_patterns = language_config.get("error_check", [])

        # 检查标准输出和标准错误输出
        output_to_check = run_result["stdout"] + run_result["stderr"]

        for pattern in error_patterns:
            if pattern in output_to_check:
                logger.warning(
                    f"src_uid: {language_config['src_uid']}, 检测到错误模式 '{pattern}' 在输出中: {language_config['lang']}"
                )
                return True

        return False

    def _handle_dotnet(self, language_config, src_uid, code_dir, timeout):
        """处理.NET语言（C#, F#, VB）- 环境已在build_code_env中设置"""
        result = {"language": language_config["lang"], "path": language_config["code_path"]}
        cwd = os.path.dirname(language_config["project_path"])
        # 获取项目信息（已在build_code_env中设置）
        project_name = language_config.get("project_name", "MyProject")

        # 编译（如果配置了编译命令）
        compile_cmd = language_config.get("compile_cmd")
        if compile_cmd:
            compile_flags = language_config.get("compile_flags", [])
            processed_compile_flags = []
            for flag in compile_flags:
                if isinstance(flag, str):
                    flag = flag.format(project_name=project_name)
                processed_compile_flags.append(flag)

            compile_command = [compile_cmd] + processed_compile_flags
            compile_result = subprocess.run(compile_command, timeout_seconds=timeout, cwd=cwd)
            logger.info(
                f"src_uid: {src_uid}, {language_config['lang']} 编译结果, 标准输出: {compile_result['stdout']}, 错误输出: {compile_result['stderr']}, 返回码: {compile_result['exit_code']}, process_cpu_util: {compile_result['process_cpu_util']} %, process_cpu_time: {compile_result['process_cpu_time']} s, process_peak_memory: {compile_result['process_peak_memory']} kB"
            )
            result.update(compile_result)
            if compile_result["timeout"]:
                result["outcome"] = ExecOutcome.TIME_LIMIT_EXCEEDED.name
                return result
            elif compile_result["exit_code"] != 0:
                result["outcome"] = ExecOutcome.COMPILATION_ERROR.name
                return result

        # 执行
        execute_cmd = language_config.get("execute_cmd")
        execute_flags = language_config.get("execute_flags", [])

        processed_execute_flags = []
        for flag in execute_flags:
            if isinstance(flag, str):
                flag = flag.format(project_name=project_name)
            processed_execute_flags.append(flag)

        run_command = [execute_cmd] + processed_execute_flags
        run_result = subprocess.run(run_command, timeout_seconds=timeout, cwd=cwd)
        logger.info(
            f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 标准输出: {run_result['stdout']}, 错误输出: {run_result['stderr']}, 返回码: {run_result['exit_code']}, process_cpu_util: {run_result['process_cpu_util']} %, process_cpu_time: {run_result['process_cpu_time']} s, process_peak_memory: {run_result['process_peak_memory']} kB"
        )

        result.update(run_result)
        self._update_run_result(language_config, run_result, result)

        return result

    def _handle_erlang(self, language_config, src_uid, code_dir, timeout):
        """处理Erlang语言 - 环境已在build_code_env中设置"""
        result = {"language": language_config["lang"], "path": language_config["code_path"]}

        # 获取已设置的文件路径和模块名
        erlang_file = language_config.get("execution_path", language_config["code_path"])
        module_name = language_config.get(
            "module_name", os.path.splitext(os.path.basename(language_config["code_path"]))[0]
        )

        # 编译
        compile_cmd = language_config.get("compile_cmd", "erlc")
        compile_flags = language_config.get("compile_flags", [])
        compile_command = [compile_cmd] + compile_flags + [erlang_file]
        compile_result = subprocess.run(compile_command, timeout_seconds=timeout, cwd=code_dir)
        logger.info(
            f"src_uid: {src_uid}, {language_config['lang']} 编译结果, 标准输出: {compile_result['stdout']}, 错误输出: {compile_result['stderr']}, 返回码: {compile_result['exit_code']}, process_cpu_util: {compile_result['process_cpu_util']} %, process_cpu_time: {compile_result['process_cpu_time']} s, process_peak_memory: {compile_result['process_peak_memory']} kB"
        )
        result.update(compile_result)
        if compile_result["timeout"]:
            result["outcome"] = ExecOutcome.TIME_LIMIT_EXCEEDED.name
            return result
        elif compile_result["exit_code"] != 0:
            result["outcome"] = ExecOutcome.COMPILATION_ERROR.name
            return result

        # 执行
        execute_cmd = language_config.get("execute_cmd", "erl")
        execute_flags = language_config.get("execute_flags", [])

        # 处理执行参数中的模块名和测试函数
        processed_execute_flags = []
        for flag in execute_flags:
            if isinstance(flag, str):
                flag = flag.format(module_name=module_name, test_function="test")
            processed_execute_flags.append(flag)

        run_command = [execute_cmd] + processed_execute_flags
        run_result = subprocess.run(run_command, timeout_seconds=timeout, cwd=code_dir)
        logger.info(
            f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 标准输出: {run_result['stdout']}, 错误输出: {run_result['stderr']}, 返回码: {run_result['exit_code']}, process_cpu_util: {run_result['process_cpu_util']} %, process_cpu_time: {run_result['process_cpu_time']} s, process_peak_memory: {run_result['process_peak_memory']} kB"
        )

        result.update(run_result)
        self._update_run_result(language_config, run_result, result)

        return result

    def _handle_go(self, language_config, src_uid, code_dir, timeout):
        """处理Go语言"""
        result = {"language": language_config["lang"], "path": language_config["code_path"]}
        # 指定go的执行目录，实现不能联网的情况下也能正常运行
        go_cwd = "/home/sandbox/go/init"
        if language_config["go_test_method"] == "test":
            run_command = ["go", "test", language_config["code_path"]]
            run_result = subprocess.run(run_command, timeout_seconds=timeout, cwd=go_cwd)
            logger.info(
                f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 标准输出: {run_result['stdout']}, 错误输出: {run_result['stderr']}, 返回码: {run_result['exit_code']}, process_cpu_util: {run_result['process_cpu_util']} %, process_cpu_time: {run_result['process_cpu_time']} s, process_peak_memory: {run_result['process_peak_memory']} kB"
            )
            result.update(run_result)
            self._update_run_result(language_config, run_result, result)
            return result
        else:
            # go的执行也在go_cwd目录, 如果要写文件的话就会报错，暂时没遇到这种场景，如果有问题后续再优化
            return self._execute_generic(
                language_config, src_uid, code_dir, timeout, go_cwd
            )

    def _handle_java(self, language_config, src_uid, code_dir, timeout):
        """处理Java语言 - 环境已在build_code_env中设置"""
        execute_result = self._execute_generic(
            language_config, src_uid, code_dir, timeout, code_dir
        )
        # Always try to parse JUnit XML (it is written even when tests fail / exit_code 1)
        try:
            xml_file = os.path.join(code_dir, 'reports', 'TEST-junit-jupiter.xml')
            tree = ET.parse(xml_file)
            root = tree.getroot()
            suite = root.find('testsuite') if root.find('testsuite') else root

            total_tests = int(suite.attrib.get('tests', 0))
            failures = int(suite.attrib.get('failures', 0))
            errors = int(suite.attrib.get('errors', 0))
            skipped = int(suite.attrib.get('skipped', 0))
            time_val = float(suite.attrib.get('time', 0))
            if total_tests == 0:
                execute_result["outcome"] = ExecOutcome.RUNTIME_ERROR.name
                execute_result["exec_runtime_message"] = "No tests found"
                execute_result["exec_test_output"] = "No tests found"
                return execute_result
            failed = failures + errors
            succeeded = total_tests - failed - skipped
            lines = []
            for tc in suite.findall("testcase"):
                name = tc.attrib.get("name", "?")
                fail = tc.find("failure")
                err_el = tc.find("error")
                if fail is not None:
                    msg = (fail.text or "").strip() or (fail.attrib.get("message", ""))[:200]
                    lines.append(f"{name}: failed - {msg}")
                elif err_el is not None:
                    msg = (err_el.text or "").strip() or (err_el.attrib.get("message", ""))[:200]
                    lines.append(f"{name}: error - {msg}")
                else:
                    lines.append(f"{name}: passed")
            execute_result["exec_test_output"] = "\n".join(lines)
            if failed > 0:
                execute_result["outcome"] = ExecOutcome.WRONG_ANSWER.name
            elif succeeded > 0:
                execute_result["outcome"] = ExecOutcome.PASSED.name
            else:
                execute_result["outcome"] = ExecOutcome.RUNTIME_ERROR.name
            summary = f"Total tests: {total_tests}, Successful: {succeeded}, Failed: {failed}, Skipped: {skipped}, Time: {time_val:.3f}s"
            execute_result["exec_runtime_message"] = summary
            if failed > 0:
                logger.error(f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 错误: {failed} test(s) failed")
            return execute_result
        except Exception as e:
            # No XML or parse error: use stdout (JUnit prints failures there) and classify by content
            stdout = (execute_result.get("stdout") or "").strip()
            stderr = (execute_result.get("stderr") or "").strip()
            exec_msg = execute_result.get("exec_runtime_message", "")
            execute_result["exec_test_output"] = stdout or stderr or exec_msg or execute_result.get("outcome", "UNKNOWN")
            if "AssertionFailedError" in stdout or "Failures (" in stdout or "AssertionError" in stdout:
                execute_result["outcome"] = ExecOutcome.WRONG_ANSWER.name
            logger.error(f"src_uid: {src_uid}, {language_config['lang']} 执行结果, 错误: {exec_msg or str(e)}")
        return execute_result

if __name__ == "__main__":
    executor = LanguageExecutor()