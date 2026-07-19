from app.executors.base import ExecutionBackend
from app.executors.file_executor import file_read, file_search
from app.executors.http_executor import execute_http
from app.executors.python_executor import python_run
from app.executors.script_executor import script_run
from app.executors.sandbox_exec import sandbox_exec
from app.executors.pcap_executor import pcap_metadata, pcap_protocols, pcap_query
from app.executors.ctf_tools import archive_list, content_discovery, file_type, http_extract, http_session_set_cookie_ref, js_asset_analyze, jwt_clone_claims, jwt_inspect, jwt_sign, pcap_placeholder, session_inspect, session_list_secret_refs, source_map_analyze, strings_extract, whatweb_fingerprint
from app.models import JobRequest


class KaliVmExecutionBackend(ExecutionBackend):
    async def execute(self, request: JobRequest) -> dict:
        handlers = {"http_request": execute_http, "http_session_request": execute_http, "http_extract": http_extract, "session_inspect": session_inspect, "session_list_secret_refs": session_list_secret_refs, "jwt_clone_claims": jwt_clone_claims, "jwt_sign": jwt_sign, "http_session_set_cookie_ref": http_session_set_cookie_ref, "whatweb_fingerprint": whatweb_fingerprint, "js_asset_analyze": js_asset_analyze, "source_map_analyze": source_map_analyze, "file_type": file_type, "strings_extract": strings_extract, "archive_list": archive_list, "file_read": file_read, "file_search": file_search, "python_run": python_run, "script_run": script_run, "sandbox_exec": sandbox_exec, "content_discovery": content_discovery, "jwt_inspect": jwt_inspect, "pcap_metadata": pcap_metadata, "pcap_protocols": pcap_protocols, "pcap_query": pcap_query, "pcap_tcp_stream": pcap_placeholder, "pcap_http_objects": pcap_placeholder, "pcap_dns_summary": pcap_placeholder, "pcap_credentials": pcap_placeholder, "sqlmap_detect": pcap_placeholder, "nmap_service_probe": pcap_placeholder, "nikto_scan": pcap_placeholder, "binwalk_scan": pcap_placeholder, "exiftool_metadata": pcap_placeholder}
        return await handlers[request.tool](request)

    async def cancel(self, job_id: str) -> None:
        return None


class DockerExecutionBackend(ExecutionBackend):
    async def execute(self, request: JobRequest) -> dict:
        raise NotImplementedError("Docker sandbox is reserved for a future release")

    async def cancel(self, job_id: str) -> None:
        return None
