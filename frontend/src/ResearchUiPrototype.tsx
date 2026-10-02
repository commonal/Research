import { useState } from "react";

type PrototypeVariant = "A" | "B" | "C";
type AnswerMode = "auto" | "direct" | "web" | "paper" | "research";

const modes: Array<{ value: AnswerMode; label: string; hint: string }> = [
  { value: "auto", label: "自动选择", hint: "由研究助理判断最合适的路径" },
  { value: "direct", label: "普通回答", hint: "使用常识和已有上下文直接回答" },
  { value: "web", label: "网页搜索", hint: "检索网页上的最新信息" },
  { value: "paper", label: "当前论文", hint: "只基于当前论文和证据块" },
  { value: "research", label: "深度研究", hint: "拆解问题、检索并形成研究产物" },
];

const traceEvents = [
  { step: "01", title: "规划研究步骤", detail: "识别问题范围与需要补充的证据" },
  { step: "02", title: "读取当前论文", detail: "读取 8 个已持久化证据块" },
  { step: "03", title: "网页检索", detail: "准备查询 beta 敏感性与 KL 约束" },
  { step: "04", title: "整理阶段结果", detail: "区分已确认事实、机制外推和待验证问题" },
];

function ModePicker({ mode, onChange }: { mode: AnswerMode; onChange: (mode: AnswerMode) => void }) {
  const selected = modes.find((item) => item.value === mode) ?? modes[0];
  return (
    <label className="research-prototype-mode-picker">
      <span>回答方式</span>
      <select aria-label="回答方式" value={mode} onChange={(event) => onChange(event.target.value as AnswerMode)}>
        {modes.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
      <small>{selected.hint}</small>
    </label>
  );
}

function TraceCards({ compact = false }: { compact?: boolean }) {
  return (
    <section className={`research-prototype-trace ${compact ? "compact" : ""}`} aria-label="公开运行轨迹">
      <header>
        <div><span className="research-prototype-eyebrow">公开运行轨迹</span><strong>研究助理正在处理</strong></div>
        <span className="research-prototype-trace-count">4 步</span>
      </header>
      <div className="research-prototype-trace-list">
        {traceEvents.map((event) => (
          <details key={event.step} className="research-prototype-trace-card">
            <summary><span>{event.step}</span><strong>{event.title}</strong><em>已完成</em></summary>
            <p>{event.detail}</p>
            <small>工具调用详情已折叠，展开查看参数与返回结果</small>
          </details>
        ))}
      </div>
    </section>
  );
}

function WorkspaceList({ active = "DPO 与 RLHF" }: { active?: string }) {
  const [readingExpanded, setReadingExpanded] = useState(false);
  return (
    <nav className="research-prototype-workspace-list" aria-label="研究工作区">
      <div className="research-prototype-workspace-head"><span className="research-prototype-eyebrow">WORKSPACES</span><button type="button">＋</button></div>
      <button className="research-prototype-workspace-group active" type="button"><span>研究项目</span><small>3 个会话</small></button>
      <div className="research-prototype-session-list">
        <button className="research-prototype-session active" type="button"><strong>{active}</strong><span>当前研究 · 1 篇论文</span></button>
        <button className="research-prototype-session" type="button"><strong>LLM 推荐系统进展</strong><span>网页检索 · 昨天</span></button>
        <button className="research-prototype-session" type="button"><strong>论文选区阅读</strong><span>选区问答 · 3 天前</span></button>
      </div>
      <button className="research-prototype-workspace-group" type="button" aria-expanded={readingExpanded} onClick={() => setReadingExpanded((value) => !value)}><span>{readingExpanded ? "⌄" : "›"} 论文阅读</span><small>2 个会话</small></button>
      {readingExpanded && <div className="research-prototype-session-list research-prototype-secondary-sessions">
        <button className="research-prototype-session" type="button"><strong>论文方法复现</strong><span>1 篇论文 · 上周</span></button>
        <button className="research-prototype-session" type="button"><strong>选区阅读与解释</strong><span>1 篇论文 · 3 天前</span></button>
      </div>}
    </nav>
  );
}

function ChatContent({ mode, onModeChange }: { mode: AnswerMode; onModeChange: (mode: AnswerMode) => void }) {
  return (
    <section className="research-prototype-chat" aria-label="研究对话">
      <header className="research-prototype-chat-head">
        <div><span className="research-prototype-eyebrow">CURRENT SESSION</span><h2>DPO 与 RLHF 的核心差异</h2><p>围绕当前研究焦点继续推进，所有阶段结果都会保存在 Workspace。</p></div>
        <button type="button" className="research-prototype-quiet-button">打开论文</button>
      </header>
      <div className="research-prototype-messages">
        <article className="research-prototype-message user"><span>你</span><p>β 变化会如何改变结果？DPO 的隐式 KL 能否防止 reward hacking？</p></article>
        <article className="research-prototype-message assistant"><span>Research Pulse</span><div><h3>阶段结论</h3><p>当前论文只报告 β=0.01，没有提供敏感性分析，也没有测量隐式 KL、策略漂移或 reward hacking。</p><div className="research-prototype-answer-grid"><div><strong>已确认</strong><p>论文使用推荐模型对候选推理排序，再用 DPO 构造偏好对。</p></div><div><strong>尚不能确定</strong><p>β 与真实推荐指标、KL 漂移之间的关系需要补充实验。</p></div></div><details className="research-prototype-inline-details"><summary>查看证据依据 · 9 个证据</summary><p>证据块和原文定位会在这里展开，引用数字仍可回跳论文页码。</p></details></div></article>
      </div>
      <footer className="research-prototype-composer">
        <ModePicker mode={mode} onChange={onModeChange} />
        <textarea aria-label="研究问题" rows={2} placeholder="继续追问，或让研究助理执行下一步…" defaultValue="" />
        <button type="button" className="research-prototype-send">发送 →</button>
      </footer>
    </section>
  );
}

function FocusPanel() {
  return (
    <aside className="research-prototype-focus" aria-label="研究设定">
      <header><span className="research-prototype-eyebrow">RESEARCH CONTEXT</span><button type="button" className="research-prototype-quiet-button">编辑</button></header>
      <section><span className="research-prototype-label">总研究问题</span><h3>DPO 与 RLHF 的核心差异</h3><p>研究 DPO 的偏好对齐机制、KL 约束和推荐反馈之间的关系。</p></section>
      <section className="research-prototype-focus-card"><span className="research-prototype-label">当前研究焦点</span><strong>β 敏感性与隐式 KL 的漂移抑制作用</strong><small>已确认 · 可继续深入</small></section>
      <section><span className="research-prototype-label">研究产物</span><button className="research-prototype-artifact" type="button"><strong>阶段结果</strong><span>已整理 · 9 个证据</span></button><button className="research-prototype-artifact" type="button"><strong>相关论文</strong><span>3 篇待核验候选</span></button><button className="research-prototype-artifact" type="button"><strong>研究计划</strong><span>2 个待执行实验</span></button></section>
    </aside>
  );
}

function VariantA({ mode, onModeChange }: { mode: AnswerMode; onModeChange: (mode: AnswerMode) => void }) {
  return <div className="research-prototype-layout variant-a"><aside className="research-prototype-left"><WorkspaceList /><TraceCards /></aside><ChatContent mode={mode} onModeChange={onModeChange} /><FocusPanel /></div>;
}

function VariantB({ mode, onModeChange }: { mode: AnswerMode; onModeChange: (mode: AnswerMode) => void }) {
  const [leftTab, setLeftTab] = useState<"workspace" | "trace">("workspace");
  return <div className="research-prototype-layout variant-b"><aside className="research-prototype-left"><div className="research-prototype-left-tabs"><button className={leftTab === "workspace" ? "active" : ""} type="button" onClick={() => setLeftTab("workspace")}>工作区</button><button className={leftTab === "trace" ? "active" : ""} type="button" onClick={() => setLeftTab("trace")}>运行轨迹</button></div>{leftTab === "workspace" ? <WorkspaceList /> : <TraceCards compact />}</aside><ChatContent mode={mode} onModeChange={onModeChange} /><FocusPanel /></div>;
}

function VariantC({ mode, onModeChange }: { mode: AnswerMode; onModeChange: (mode: AnswerMode) => void }) {
  return <div className="research-prototype-layout variant-c"><aside className="research-prototype-left"><div className="research-prototype-project-switcher"><strong>研究项目</strong><button type="button">切换</button></div><WorkspaceList active="DPO 与 RLHF" /><TraceCards compact /></aside><main className="research-prototype-main"><ChatContent mode={mode} onModeChange={onModeChange} /><div className="research-prototype-bottom-context"><FocusPanel /></div></main></div>;
}

export function ResearchUiPrototype() {
  const params = new URLSearchParams(window.location.search);
  const initial = (params.get("variant")?.toUpperCase() as PrototypeVariant | null);
  const [variant, setVariant] = useState<PrototypeVariant>(initial === "B" || initial === "C" ? initial : "A");
  const [mode, setMode] = useState<AnswerMode>("auto");
  const labels: Record<PrototypeVariant, string> = { A: "三栏研究台", B: "双层左栏", C: "对话优先" };
  const switchVariant = (next: PrototypeVariant) => {
    setVariant(next);
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("prototype", "research-ui");
    nextUrl.searchParams.set("variant", next);
    window.history.replaceState({}, "", nextUrl);
  };
  return (
    <main className={`research-prototype research-prototype-${variant}`}>
      <header className="research-prototype-topbar"><div><span className="research-prototype-logo">R</span><strong>Research Pulse</strong><span className="research-prototype-badge">UI 原型 · 不写入真实数据</span></div><span>研究工作台</span></header>
      {variant === "A" && <VariantA mode={mode} onModeChange={setMode} />}
      {variant === "B" && <VariantB mode={mode} onModeChange={setMode} />}
      {variant === "C" && <VariantC mode={mode} onModeChange={setMode} />}
      <nav className="research-prototype-switcher" aria-label="原型方案切换"><button type="button" onClick={() => switchVariant(variant === "A" ? "C" : variant === "B" ? "A" : "B")}>‹</button><span>方案 {variant} · {labels[variant]}</span><button type="button" onClick={() => switchVariant(variant === "A" ? "B" : variant === "B" ? "C" : "A")}>›</button></nav>
    </main>
  );
}
