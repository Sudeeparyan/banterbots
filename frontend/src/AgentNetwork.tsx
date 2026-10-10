import type { CSSProperties } from 'react';
import { ArrowRight, Check, LoaderCircle, Network, Play, RefreshCw, X } from 'lucide-react';
import type { AgentFlow, AgentId, FlowStep, Party } from './broadcast';

type Names = Record<AgentId, string>;

const firstName = (name: string) => name.split(' ')[0];
const shortTask = (id?: string) => (id ? id.replace(/-/g, '').slice(0, 8) : undefined);
const where = (url?: string) => {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.port ? `port ${parsed.port}` : parsed.host;
  } catch {
    return url;
  }
};
const seconds = (ms?: number) =>
  ms == null ? undefined : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

/** One plain-English sentence per step, so the A2A route reads like a conversation. */
export function stepSentence(step: FlowStep, names: Names): string {
  const who = (party: Party) => (party === 'studio' ? 'Studio' : firstName(names[party]));
  switch (step.key) {
    case 'deliver':
      return `Studio sends the play to ${who(step.to)}`;
    case 'call':
      return `${who(step.from)} writes the opening call`;
    case 'ask':
      return `${who(step.from)} sends that call to ${who(step.to)} and asks for a reply`;
    case 'reply':
      return `${who(step.from)} replies to ${who(step.to)}`;
    case 'return':
      return `${who(step.from)} hands both lines back to the studio`;
  }
}

function stepDetail(step: FlowStep): string | undefined {
  const task = shortTask(step.taskId);
  if (step.key === 'deliver' || step.key === 'ask') return task && `A2A task ${task}`;
  if (step.key === 'reply')
    return step.latencyMs != null ? `answered in ${seconds(step.latencyMs)}` : undefined;
  if (step.key === 'return')
    return step.latencyMs != null ? `whole exchange ${seconds(step.latencyMs)}` : undefined;
  return undefined;
}

function Marker({ step, index }: { step: FlowStep; index: number }) {
  if (step.state === 'done') return <Check size={13} strokeWidth={3} />;
  if (step.state === 'active') return <LoaderCircle size={13} className="spin" />;
  if (step.state === 'failed') return <X size={13} strokeWidth={3} />;
  return <>{index + 1}</>;
}

export function AgentNetwork({
  flow,
  names,
  colors,
  urls,
  health,
  checkedAgo,
  connected,
  readingSaved,
  replies,
  canTest,
  onTest,
  onRecheck,
}: {
  flow: AgentFlow | null;
  names: Names;
  colors: Record<AgentId, string | undefined>;
  urls?: Record<AgentId, string>;
  health: Record<AgentId, boolean> | null;
  checkedAgo: string;
  connected: boolean;
  readingSaved: boolean;
  replies: number;
  canTest: boolean;
  onTest: () => void;
  onRecheck: () => void;
}) {
  const active = flow?.steps.find((step) => step.state === 'active');
  const offline = (['a', 'b'] as const).filter((agent) => health && !health[agent]);
  const studioUp = connected || readingSaved;
  const allGood = studioUp && health?.a && health?.b;
  const summary = readingSaved
    ? 'Viewing a saved run'
    : !health
      ? 'Checking services…'
      : !connected
        ? 'Studio link is down'
        : offline.length
          ? `${offline.map((agent) => firstName(names[agent])).join(' and ')} not responding`
          : 'All systems working';
  const nodes: {
    id: Party;
    name: string;
    role: string;
    address: string;
    up: boolean | null;
  }[] = [
    {
      id: 'studio',
      name: 'Studio',
      role: 'Coordinator',
      address: readingSaved ? 'saved run' : connected ? 'connected' : 'reconnecting',
      up: studioUp,
    },
    ...(['a', 'b'] as const).map((agent) => ({
      id: agent,
      name: firstName(names[agent]),
      role: `Agent ${agent.toUpperCase()}`,
      address: where(urls?.[agent]) ?? 'separate service',
      up: health ? health[agent] : null,
    })),
  ];
  const headline = !flow
    ? 'No messages yet'
    : flow.status === 'running'
      ? `${firstName(names[flow.lead])} and ${firstName(names[flow.peer])} are talking`
      : flow.status === 'done'
        ? `${firstName(names[flow.lead])} and ${firstName(names[flow.peer])} exchanged messages`
        : flow.status === 'failed'
          ? 'This exchange did not finish'
          : 'Paused before the exchange finished';
  return (
    <section className="panel network-panel" aria-labelledby="network-title">
      <div className="panel-header">
        <span id="network-title">
          <Network size={15} /> AGENT NETWORK
        </span>
        <span className={`network-summary ${allGood ? 'ok' : readingSaved ? '' : 'warn'}`}>
          <i />
          {summary}
        </span>
      </div>
      <div className="network-nodes">
        {nodes.map((node, i) => (
          <div
            key={node.id}
            className={`network-node ${node.up === false ? 'down' : node.up ? 'up' : ''} ${active?.from === node.id ? 'busy' : ''}`}
            style={
              node.id !== 'studio' && colors[node.id]
                ? ({ '--node': colors[node.id] } as CSSProperties)
                : undefined
            }
          >
            {i > 0 && <ArrowRight className="network-link" size={12} aria-hidden="true" />}
            <span className="network-dot" aria-hidden="true" />
            <strong>{node.name}</strong>
            <span>{node.role}</span>
            <small>{node.up === false ? 'not responding' : node.address}</small>
          </div>
        ))}
      </div>
      <div className="network-check">
        <span>
          {replies
            ? `${replies} agent-to-agent ${replies === 1 ? 'reply' : 'replies'} so far`
            : flow?.status === 'running'
              ? 'First exchange in progress'
              : 'Agents have not talked yet'}
          <small> · checked {checkedAgo}</small>
        </span>
        <button onClick={onRecheck} aria-label="Check agent services again">
          <RefreshCw size={12} /> Check again
        </button>
      </div>
      <div className={`network-flow ${flow?.status ?? 'empty'}`}>
        <p className="network-headline">
          {headline}
          {flow?.recorded && <small> · from the saved transcript</small>}
        </p>
        {flow ? (
          <ol className="flow-steps">
            {flow.steps.map((step, i) => {
              const detail = stepDetail(step);
              return (
                <li key={step.key} className={`flow-step ${step.state}`}>
                  <span className="flow-marker">
                    <Marker step={step} index={i} />
                  </span>
                  <span>
                    {stepSentence(step, names)}
                    {detail && <small>{detail}</small>}
                  </span>
                </li>
              );
            })}
          </ol>
        ) : (
          <div className="network-empty">
            <p>
              Send one play and watch the studio hand it to an agent, who then messages the other
              agent for a reply.
            </p>
            <button disabled={!canTest} onClick={onTest}>
              <Play size={13} fill="currentColor" /> Send a test play
            </button>
          </div>
        )}
      </div>
      <p className="network-note">
        Each agent is a separate program. Every arrow above is a real web request between them using
        the A2A protocol. Open <b>Agent debugger</b> to see the raw messages.
      </p>
    </section>
  );
}

export function StudioGuide({ onClose }: { onClose: () => void }) {
  return (
    <section className="studio-guide" aria-label="How BanterBots works">
      <div className="guide-intro">
        <span>NEW HERE?</span>
        <strong>How it works</strong>
      </div>
      <ol>
        <li>
          <b>1</b>
          <span>
            <strong>Pick a real play</strong>
            Choose an Instant Replay moment or press Start broadcast. Both agents get the same
            facts.
          </span>
        </li>
        <li>
          <b>2</b>
          <span>
            <strong>Two AI commentators react</strong>
            Max backs the home team and Riley backs the visitors. Each runs as its own agent.
          </span>
        </li>
        <li>
          <b>3</b>
          <span>
            <strong>They message each other</strong>
            One agent calls the play, then sends it to the other for a reply. Agent network shows
            each message.
          </span>
        </li>
      </ol>
      <button className="icon-button" aria-label="Hide how it works" onClick={onClose}>
        <X size={16} />
      </button>
    </section>
  );
}
