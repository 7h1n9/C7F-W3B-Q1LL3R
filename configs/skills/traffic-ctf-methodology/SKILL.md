---
name: traffic-ctf-methodology
display_name: Traffic CTF Methodology
description: Methodology skill for authorized traffic-analysis CTF challenges.
skill_kind: METHODOLOGY
activation_mode: AUTO
challenge_types: [TRAFFIC_ANALYSIS]
required_tools: [pcap_metadata, pcap_protocols, pcap_query]
recommended_tools: [pcap_metadata, pcap_protocols, pcap_query, file_read, file_search, python_run]
ctf_phases: [BASELINE, MAPPING, TESTING, FLAG_SEARCH, FLAG_VERIFICATION, REPORTING]
triggers: [pcap, tcp, udp, dns, http, tls, auth, credential, flag]
---

# Traffic CTF Methodology

Start by learning the capture shape, then narrow the protocol graph before running targeted queries.

## Focus

- Read PCAP metadata first.
- Map the dominant protocols and endpoints.
- Run a narrow query once the protocol path is clear.
- Keep the evidence trail short and auditable.

## When the capture resists analysis

- If a filter returns no new information, widen only one step at a time.
- Pivot from packet contents to metadata, then to endpoint mapping, then back to packet details.
- If the protocol path is still unclear, document the unknowns and avoid blind broad scans.
- Treat a dead query as a rejected path, not as a signal to repeat it.
