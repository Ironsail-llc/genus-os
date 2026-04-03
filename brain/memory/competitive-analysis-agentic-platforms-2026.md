# Competitive Analysis: Agentic AI Platforms (March 2026)

Research date: 2026-03-30. Compiled for Genus OS / Robothor gap analysis.

---

## 1. MANUS AI (Meta)

**What it is:** General-purpose autonomous AI agent launched March 2025 by Butterfly Effect (Singapore). Acquired by Meta for ~$2B in December 2025. Hit ~$125M ARR within 8 months. Desktop app ("My Computer") launched March 2026.

### Architecture
- **Multi-model:** Claude 3.5 Sonnet as primary reasoning + Alibaba Qwen fine-tuned models for specific tasks. Multi-model dynamic invocation routes tasks to specialized models.
- **CodeAct paradigm:** Agent actions are executable Python code, not fixed tool tokens. Enables conditional logic, library use, and multi-tool composition in single steps.
- **Sandbox:** Each task gets a dedicated Firecracker microVM (full Ubuntu Linux, Python 3.10, Node.js 20, sudo, Chromium browser, VS Code). Sessions can persist for hours.
- **Multi-agent:** Planner agent decomposes goals into ordered steps → Executor agents run in parallel sandboxes → Verification agent validates results. Wide Research mode spawns up to 100 parallel sub-agents.

### Memory / Context
- **Event stream:** Chronological log of all interactions/actions/observations fed to the LLM.
- **File-based persistence:** Agents save intermediate results to disk rather than relying solely on context window. Todo.md tracks plan progress.
- **Knowledge module:** External knowledge base for domain-specific guidance injected as context events.
- **RAG:** Vector-based retrieval for external documents.
- **Cross-session:** Retains user preferences and past interactions across sessions.

### Tool Ecosystem
- 29+ purpose-built tools: web search, browser automation (adapted from open-source "browser use"), shell commands, file I/O, code execution, API calls.
- Integrations: Gmail, Notion, HubSpot, and growing.
- Datasource module provides pre-approved APIs (weather, finance) prioritized over web scraping.
- Desktop app can interact with local apps, IDEs (Xcode, Python, Node.js), terminal, local GPU.

### Enterprise Features
- **Team plan:** SSO, shared credit pools, team analytics, internal access controls, shared templates.
- **Pricing:** $19/mo (Starter) → $199/mo (Premium), credit-based consumption. Team plans: 4,000 credits/seat/month.
- **Deployment:** Cloud-first SaaS. New desktop app adds hybrid architecture (local orchestration + cloud inference). No self-hosted option yet.
- **Compliance:** No public SOC 2/HIPAA claims yet.

### Self-Healing / Self-Improvement
- Error handling rules in system prompt: self-diagnosis, retry strategies, fallback approaches.
- Manus 1.6 Max improved one-shot task success rate; tasks complete autonomously without human intervention more often.

### Voice / Multimodal
- Screenshot-based browser interaction (text content + viewport screenshot + bounding box overlay for clickable elements).
- No native voice capabilities documented.

### CRM / Business Process
- HubSpot integration. No deep native CRM.
- Meta Ads Manager integration launched (4M+ advertisers) for campaign analysis, audience research, reporting.

### Key Benchmarks
- GAIA benchmark: Exceeded previous leaderboard champion (65%), outperformed GPT-4 and OpenAI Deep Research by 10%+.
- Task completion times: Dropped from ~15 min to <4 min after October 2025 re-architecture.

---

## 2. OPENAI — Operator / ChatGPT Agent / Agents SDK

**What it is:** Multiple agent products: Operator (launched Jan 2025, deprecated Aug 2025), ChatGPT Agent (replaced Operator), Agents SDK (developer framework), Codex (coding agent).

### Architecture
- **CUA Model (Computer-Using Agent):** GPT-4o vision + reinforcement learning for GUI interaction. Sees screenshots, uses mouse/keyboard.
- **ChatGPT Agent:** Unified Operator's web interaction + Deep Research's synthesis + ChatGPT's conversational intelligence.
- **Agents SDK:** Open-source Python/TypeScript SDK. Same patterns as deprecated Swarm but production-grade.
- **Responses API:** Single interface unifying chat completions + tool execution.

### Multi-Agent / Orchestration
- **Handoffs:** First-class primitive for transferring control between specialized agents.
- **Guardrails:** Input/output validation, policy enforcement, content moderation — run in parallel with agent execution.
- **Tracing:** Built-in visualization, debugging, monitoring of multi-step workflows.

### Memory / Context
- **Sessions API:** Persistent memory layer. Session becomes the memory object — SDK handles context length, history, continuity automatically.
- No native long-term memory / knowledge graph documented.

### Enterprise Features
- Enterprise and Education tiers for ChatGPT Agent.
- SOC 2 Type 2, GDPR compliant.
- Rumored Ph.D.-level agent: $2,000-$20,000/month for enterprise.
- **Pricing (API):** $3/1M input tokens, $12/1M output tokens.
- No self-hosted option. Cloud only.

### Self-Healing
- CUA self-corrects when encountering challenges. Breaks tasks into multi-step plans and adapts.

### Voice / Multimodal
- GPT-4o natively multimodal (text, image, audio, video).
- Advanced Voice Mode in ChatGPT.
- Computer use via screenshots.

### CRM / Business Process
- No native CRM. Relies on third-party integrations.
- No native SaaS connector normalization.

---

## 3. GOOGLE — Project Mariner / Gemini Agents / ADK

**What it is:** Multiple agent products: Project Mariner (browser agent), Vertex AI Agent Builder (enterprise), Agent Development Kit (ADK, open-source framework), Agentspace (enterprise hub).

### Architecture
- **Project Mariner:** Built on Gemini 2.0. Multi-agent system executing up to 10 parallel tasks. WebVoyager benchmark: 83.5%.
- **ADK:** Open-source, model-agnostic framework. Production agents in <100 lines of Python. ADK 2.0 Alpha adds graph-based workflows.
- **Agent Engine (Vertex AI):** Managed deployment, scaling, security, monitoring for production agents.

### Multi-Agent / Orchestration
- ADK 2.0: Graph-based workflow orchestration.
- Agent Designer: Low-code visual canvas for orchestrating agents and sub-agents, exportable to ADK code.
- A2A (Agent-to-Agent) protocol support.
- MCP (Model Context Protocol) support.

### Memory / Context
- ADK: Native state management with failure recovery and human-in-the-loop pause/resume.
- No public details on long-term persistent memory architecture.

### Enterprise Features
- **Vertex AI Agent Builder:** Enterprise-grade with tool governance via Cloud API Registry (admin-curated approved tools).
- **Planned roadmap:** Enterprise API (Q1 2026), Mariner Studio visual builder (Q2 2026), cross-device sync (Q3 2026), agent marketplace (Q4 2026).
- **Pricing:** Agent Engine runtime pricing lowered Jan 2026. Pay-per-use via GCP.
- **Deployment:** Cloud (GCP). No self-hosted option.
- Available to Google AI Ultra subscribers (US).

### Voice / Multimodal
- Gemini natively multimodal (text, image, audio, video).
- Project Mariner: Browser-native agent.
- Ray-Ban Meta glasses integration planned (via Gemini).

---

## 4. MICROSOFT — Agent Framework (AutoGen + Semantic Kernel)

**What it is:** Unified open-source framework merging AutoGen (Microsoft Research) and Semantic Kernel into the Microsoft Agent Framework. Public preview, GA targeted Q1 2026.

### Architecture
- **Agent Framework:** Combines AutoGen's simple agent abstractions with Semantic Kernel's enterprise features (session-based state, type safety, middleware, telemetry).
- **Dual orchestration:** Agent Orchestration (LLM-driven, creative) + Workflow Orchestration (deterministic, business-logic).
- **Languages:** Python and .NET.

### Multi-Agent / Orchestration
- Sequential, Concurrent, Group Chat, Handoff, and Magentic orchestration patterns.
- Magentic: Manager agent builds dynamic task ledger, coordinating specialized agents + humans.
- Graph-based workflows for explicit multi-agent execution paths.

### Memory / Context
- Session-based state management for long-running and human-in-the-loop scenarios.
- Robust state management for stateful tasks.

### Enterprise Features
- **OpenTelemetry** for observability.
- **Entra ID** authentication, security hooks.
- **Long-running durability** for stateful tasks.
- **Human-in-the-loop** approval steps for governance.
- **Responsible AI:** Task adherence, prompt shields, PII detection — built into Azure AI Foundry.
- **Integration:** OpenAPI, A2A (Agent-to-Agent), MCP.
- **Deployment:** Azure-native. Self-hosted possible (open-source). Production SLAs planned for GA.
- **GA timeline:** Q1 2026 with stable APIs, multi-language (C#, Python, Java).

### Voice / Multimodal
- Azure AI Speech integration. Copilot voice in Teams/Office.

### CRM / Business Process
- Copilot agents embedded in Office 365 (Researcher, Analyst).
- Dynamics 365 integration.
- Deepest enterprise SaaS integration of any platform.

---

## 5. LANGGRAPH / LANGCHAIN

**What it is:** Open-source agent orchestration framework. LangGraph is the runtime; LangChain is the broader ecosystem. 60% market share among AI developers building agents.

### Architecture
- Graph-based orchestration for stateful, branching, multi-agent workflows.
- Model-agnostic.
- Python and TypeScript.

### Multi-Agent / Orchestration
- Single, multi-agent, and hierarchical control flows.
- Explicit workflow graph control with error handling/recovery.
- MCP integration support.

### Memory / Context
- **Persistence layer:** Short-term working memory + long-term persistent memory across sessions.
- Conversation history + vector store retrieval.
- Human-in-the-loop and async collaboration support.

### Enterprise Features
- **LangGraph Platform (GA):** Managed deployment with three tiers:
  - Developer: Free (100K nodes/month), self-hosted only.
  - Plus: Cloud SaaS, GCP (US/EU).
  - Enterprise: Hybrid (SaaS control + self-hosted data plane) or fully self-hosted (no data leaves VPC).
- **LangSmith:** Observability, debugging, evaluation.
- **Deployment:** Cloud, hybrid, fully self-hosted.
- **Customers:** Klarna, Replit, Elastic.
- 220% GitHub star growth, 300% download growth Q1 2024 → Q1 2025.

### Self-Healing
- Durable execution: Agents persist through failures.
- Error recovery built into graph-based execution.

---

## 6. CREWAI

**What it is:** Open-source multi-agent framework. Standalone (no LangChain dependency). 100K+ certified developers. 12M+ daily executions.

### Architecture
- **Crews:** Teams of role-based agents (Manager, Worker, Researcher).
- **Flows:** Event-driven workflows for production automation.
- Built from scratch in Python. No framework dependencies.

### Multi-Agent / Orchestration
- Role-based collaboration: manager oversees, workers execute, researchers gather intel.
- Sequential and parallel task execution.
- Inter-agent communication and delegation.

### Enterprise Features
- **CrewAI AMP (enterprise):** Triggers for Gmail, Slack, Salesforce. RBAC, deployment management.
- **Compliance:** HIPAA, SOC 2.
- **Deployment:** On-premise, cloud, VPC, self-hosted K8s.
- **Pricing:** Free (open-source), $25/mo (Pro, 100 executions), custom Enterprise (up to 30K executions, self-hosted K8s/VPC).

### Memory / Context
- Agent-level memory and shared context.
- No public details on long-term persistent memory.

---

## 7. AUTOGPT / AGENTGPT

**What it is:** Open-source autonomous agent (AutoGPT, March 2023) + web-based version (AgentGPT). Pioneered the "autonomous AI agent" concept.

### Current Status (2026)
- Still active but eclipsed by newer platforms.
- Best used as semi-autonomous orchestrator with human-in-the-loop checkpoints.
- Known for getting stuck in loops, hallucination, high API costs.
- Enterprise tier available with managed hosting, priority support, security features.
- Vector database memory (AgentGPT).

### Key Limitation
- No significant enterprise adoption or self-healing improvements. Largely a proof-of-concept that inspired the industry.

---

## 8. DEVIN (Cognition Labs)

**What it is:** First "AI software engineer." Plans, codes, debugs, deploys autonomously. Cognition Labs valued at ~$4B (March 2025).

### Architecture
- Full development environment: editor, terminal, browser, debugger.
- Plans multi-step engineering tasks requiring thousands of decisions.
- Self-healing: reads error logs, iterates, fixes autonomously.

### Enterprise Features
- **Pricing:** Core $20/mo, Team $500/mo, Enterprise custom. Usage-based ACU (Agent Compute Units).
- **Customers:** Goldman Sachs pilot (12K developers, 20% efficiency gains).
- Legacy code migration (COBOL/Fortran → modern languages).
- Collaborative PRs with human code review.
- SWE-bench: 13.86% (7x improvement over previous AI models).

### Integration with Windsurf
- Cognition acquired Windsurf (Codeium) for ~$250M in Dec 2025. Combined platform: IDE-level intelligence (Windsurf Cascade) + autonomous task execution (Devin).

---

## 9. OPENHANDS (formerly OpenDevin)

**What it is:** Open-source, model-agnostic platform for AI coding agents. MIT license. 2.1K+ contributions, 188+ contributors.

### Architecture
- **AgentHub:** Registry of agent templates (CodeActAgent, BrowserAgent, Micro-agents).
- Sandboxed runtimes for code execution.
- CLI, Local GUI with REST API + React frontend, Python SDK.

### Enterprise Features
- Self-hosted in own VPC via Kubernetes.
- Source-available enterprise contracts with extended support.
- Fine-grained access control.
- Model-agnostic (Claude, GPT, any LLM).

---

## 10. WINDSURF (Cognition / ex-Codeium)

**What it is:** AI-native IDE acquired by Cognition (Devin) for ~$250M in Dec 2025. 1M+ active users. 4,000+ enterprise deployments.

### Core Features
- **Cascade:** Understands entire codebase, multi-file edits, terminal commands, auto-context from monorepos.
- **Tab/Supercomplete:** Fast inline completions.
- **Agent mode:** Autonomous multi-step coding tasks.

### Enterprise Features
- SOC 2 Type II, admin controls, RBAC.
- Cloud/hybrid/self-hosted deployment options.
- **Pricing:** Free → Pro $15/mo → Teams $30/user/mo → Enterprise $60/user/mo.
- LogRocket #1 AI Dev Tool (Feb 2026).

---

## 11. CURSOR

**What it is:** AI-native IDE. Leading agentic coding tool alongside Windsurf.

### Core Features
- **Composer:** Multi-file editing from natural language. Cursor 2.0 centered on agents, not files.
- **Agent Cloud:** Up to 8 parallel agents in isolated cloud environments.
- **Automations:** Schedule agents or trigger from external tools in cloud sandboxes.
- **Bugbot:** Auto-detects PR issues, spins up agents to test and propose fixes.
- **MCP support** for dynamic tool integration.

### Enterprise Features
- **SOC 2 Type 2**, GDPR, CCPA. AES-256, TLS 1.2+, annual pen testing.
- **Teams $40/user/mo:** Centralized billing, analytics, org-wide privacy, SAML/OIDC SSO.
- **Enterprise:** SCIM, AI code audit logs, pooled usage, priority support.

---

## 12. SALESFORCE AGENTFORCE

**What it is:** Enterprise CRM-native AI agent platform. The dominant player for CRM-specific agent automation.

### Core Features
- Pre-built agent skills for CRM, Slack, Tableau.
- MuleSoft integration for extending to any system.
- Natural language agent creation ("Onboard New Product Managers" → auto-generates agent).
- Sales Development, Sales Coaching autonomous agents.

### Enterprise Features
- **Pricing:** $125-$650/user/month + Flex Credits ($0.10/action). First-year cost ~$140K for 10-person team.
- FedRAMP High authorization option.
- Deepest native CRM integration of any agent platform.

---

## 13. OTHER NOTABLE PLATFORMS

### PwC Agent OS
- Enterprise orchestration platform connecting AI agents across Anthropic, AWS, GCP, Azure, OpenAI, Oracle, Salesforce, SAP, Workday.
- Vendor-agnostic. Modular, adaptive workflows.

### Lyzr AI (Agentic OS)
- $8M Series A. Private AI workforce for enterprises.
- Cross-department agent coordination (HR, Sales, Finance).
- Wraps open-source frameworks (LangChain, etc.) in governance layer.
- On-premise deployment.

### n8n
- Self-hosted workflow automation with AI agent nodes.
- 500+ integrations. Free Community Edition, no execution limits self-hosted.
- Enterprise: SSO (SAML/LDAP), encrypted secrets, version control, RBAC.
- MCP integration (2026). Native Python execution alongside Node.js.
- AI Agent node: chain-of-thought, tool calling, memory, vector store retrieval.

### Lindy AI
- No-code AI automation platform.
- Visual workflow builder for sales, support, operations.
- CRM updates, scheduling, follow-ups without code.

### Relevance AI
- AI agent builder focused on structured data workflows.
- No-code visual interface.
- Different from Lindy — more data-science oriented.

### VAST AI OS
- AgentEngine (2026): Production-grade deployment for AI agents.
- Containerized runtimes, lifecycle management, MCP integration, full auditability.

### Xebia Agentic OS
- Enterprise AI orchestration platform.
- Consulting-driven approach.

---

## COMPARATIVE MATRIX

| Capability | Manus | OpenAI | Google | Microsoft | LangGraph | CrewAI | Devin | Cursor | Salesforce |
|---|---|---|---|---|---|---|---|---|---|
| **Multi-agent** | Yes (100 parallel) | Handoffs | 10 parallel | 5 patterns | Graph-based | Role-based | No | 8 parallel | Skills-based |
| **Self-hosted** | No (desktop hybrid) | No | No | Yes (OSS) | Yes (Enterprise) | Yes (K8s) | No | No | No |
| **Memory/persistence** | File+RAG+event stream | Sessions API | ADK state mgmt | Session-based | Short+long term | Agent-level | Session | Codebase-aware | CRM-native |
| **Voice** | No | Yes (GPT-4o) | Yes (Gemini) | Yes (Azure) | No | No | No | No | No |
| **CRM native** | HubSpot+Meta Ads | No | No | Dynamics 365 | No | Salesforce trigger | No | No | **Yes (core)** |
| **Self-healing** | Error retry | Self-correct | Failure recovery | Durability | Failure persist | No | **Yes (code)** | Bugbot autofix | No |
| **Computer use** | **Yes (VM+desktop)** | **Yes (CUA)** | **Yes (Mariner)** | No | No | No | Yes (dev env) | No | No |
| **SOC 2** | No | Yes | Yes (GCP) | Yes (Azure) | Enterprise | Yes | Unknown | Yes | Yes |
| **MCP support** | No | No | Yes | Yes | Yes | No | No | Yes | Yes (MuleSoft) |
| **A2A protocol** | No | No | Yes | Yes | No | No | No | No | No |
| **Pricing floor** | $19/mo | $3/1M tokens | GCP usage | Free (OSS) | Free (OSS) | Free (OSS) | $20/mo | Free tier | $125/user/mo |
| **Federation** | No | No | No | No | No | No | No | No | No |

---

## KEY MARKET TRENDS (2026)

1. **Consolidation wave:** Meta acquired Manus ($2B), Cognition acquired Windsurf ($250M). Expect more M&A.
2. **Desktop/local shift:** Manus "My Computer," Cursor cloud agents, Claude Code — agents moving from cloud-only to hybrid local+cloud.
3. **Protocol convergence:** MCP (Model Context Protocol) and A2A (Agent-to-Agent) emerging as interoperability standards. Google and Microsoft leading adoption.
4. **Multi-agent is table stakes:** Every major platform now supports some form of multi-agent orchestration.
5. **Enterprise governance gap:** Most platforms lack federation, multi-tenancy, and compliance features that large enterprises need. PwC Agent OS and Lyzr targeting this gap.
6. **Self-hosted demand growing:** LangGraph, CrewAI, n8n, Microsoft Agent Framework all offer self-hosted. Cloud-only platforms (Manus, OpenAI) adding local capabilities.
7. **CRM convergence:** Salesforce Agentforce dominates CRM-native agents. Other platforms rely on integrations.
8. **Gartner prediction:** 40% of enterprise apps will feature AI agents by 2026 (up from <5% in 2025).
9. **Market size:** AI agents market $12-15B (2025), projected $80-100B by 2030.

---

## ROBOTHOR GAP ANALYSIS vs. COMPETITORS

### Where Robothor is AHEAD of most competitors:
- **True self-hosted autonomy:** Runs entirely on own hardware. No cloud dependency. Most competitors are cloud-only or cloud-first.
- **Federation protocol:** Ed25519 identity, Consul-style tokens, HLC sync, NATS transport. **No competitor has anything like this.** Only Microsoft A2A and Google A2A are in the same conceptual space, but those are protocol specs, not deployed implementations.
- **Nightwatch self-improvement:** Overnight PR system with failure analysis, improvement proposals, and auto-disable safety. Only Devin's self-healing code comes close, and that's narrow to coding.
- **Deep CRM integration:** Native PostgreSQL CRM with Bridge service, contact resolution, conversation tracking. More integrated than anything except Salesforce Agentforce.
- **Voice calling (Twilio):** Inbound + outbound phone calls via Gemini Live. Most competitors have no voice. OpenAI and Google have voice but not phone-call-integrated.
- **Persistent identity:** Email, phone number, domain, home address. Robothor exists as an entity. No competitor has this.
- **Memory system depth:** Hybrid search (vector + BM25 + RRF), entity graph, fact extraction with quality gates, lifecycle management, consolidation, forgetting. Exceeds what any competitor documents publicly.

### Where Robothor has GAPS vs. competitors:
- **Computer use / GUI automation:** Manus, OpenAI CUA, and Google Mariner can all interact with GUIs via screenshots. Robothor has vision monitor but no autonomous GUI interaction agent.
- **Parallel agent execution at scale:** Manus runs 100 parallel sub-agents. Robothor's sub-agent system supports concurrent spawning with semaphore(3). Could be expanded.
- **Sandbox/VM isolation:** Manus gives each task a Firecracker microVM. Robothor agents share the host environment. Security isolation is a gap for untrusted workloads.
- **No-code / visual builder:** Several competitors (Google Agent Designer, CrewAI AMP, n8n, Cursor Automations) offer visual workflow builders. Robothor is YAML manifests + code only.
- **MCP server ecosystem:** Robothor has MCP clients but the broader MCP tool marketplace (Google, Microsoft, Cursor, LangGraph all embracing it) is growing fast.
- **Browser automation agent:** Manus, OpenAI, and Google have sophisticated browser-use agents. Robothor has web_fetch but no browser automation.
- **Mobile / cross-device:** Manus has a desktop app, Google plans cross-device sync. Robothor is server-only with Telegram as the mobile interface.
- **Multi-model routing:** Manus routes different task types to specialized models. Robothor has per-agent model assignment but not dynamic intra-task routing.
- **Formal compliance certifications:** SOC 2, HIPAA, FedRAMP. Not relevant for single-instance use but would matter for federation/multi-tenant.

### Strategic recommendations:
1. **Browser automation agent** — Highest-impact gap. Add a browser-use agent (Playwright/Puppeteer in sandbox) that can be spawned as a sub-agent.
2. **Parallel sub-agent scaling** — Increase semaphore from 3 to configurable limit. Consider process-level isolation.
3. **Computer use / GUI agent** — Leverage existing vision pipeline + Claude computer use tool for desktop automation.
4. **MCP tool marketplace** — Robothor already has MCP clients. Publishing Robothor's tools as MCP servers would enable interoperability.
5. **Dynamic model routing** — Add difficulty-aware routing within tasks (the v2 router module exists but could be expanded).
