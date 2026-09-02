import { PathText } from '../PathText';
import type { PlannerPlan } from '../../../../shared/agentTypes';

/** The complete structured plan, shared by the decision card and reading view. */
export function PlanDetails({ plan }: { plan: PlannerPlan }): React.JSX.Element {
  const goals = plan.goals || [];
  const goalLabel = (id: string): string => goals.find((goal) => goal.goal_id === id)?.title || id;
  return <div className="plan-details">
    {plan.summary && <p className="plan-summary"><PathText>{plan.summary}</PathText></p>}
    <TextSection title="Clarified scope" text={plan.clarification_summary} />
    <ListSection title="Assumptions" items={plan.assumptions} />
    <ListSection title="Dependency warnings" items={plan.dependency_errors} />
    <ListSection title="Dependency adjustments" items={plan.dependency_repairs} />
    <h3>Goals · {goals.length}</h3>
    <ol className="plan-goals">{goals.map((goal, index) => <li key={goal.goal_id || index}>
      <h4><span aria-hidden="true">{index + 1}</span><PathText>{goal.title || goal.goal || goal.goal_id}</PathText></h4>
      {goal.goal && goal.goal !== goal.title && <p><PathText>{goal.goal}</PathText></p>}
      {goal.reason && <p><strong>Why:</strong> <PathText>{goal.reason}</PathText></p>}
      <dl className="plan-goal-meta">
        {goal.estimated_scope && <><dt>Scope</dt><dd><PathText>{goal.estimated_scope}</PathText></dd></>}
        {!!goal.depends_on?.length && <><dt>Depends on</dt><dd><PathText>{goal.depends_on.map(goalLabel).join(', ')}</PathText></dd></>}
        {goal.parallelizable !== undefined && <><dt>Execution</dt><dd>{goal.parallelizable ? 'Can run in parallel' : 'Sequential'}</dd></>}
        {goal.preserve_context !== undefined && <><dt>Context</dt><dd>{goal.preserve_context ? 'Preserve previous context' : 'Fresh context'}</dd></>}
      </dl>
      <ListSection title="Success criteria" items={goal.success_signals} />
      <ListSection title="Implementation notes" items={goal.delegation_notes} />
      <ListSection title="Relevant facts" items={goal.relevant_fact_keys} />
    </li>)}</ol>
    <ListSection title="Out of scope" items={plan.not_in_scope} />
    <ListSection title="Next steps" items={plan.next_steps_preview} />
    <TextSection title="Original request" text={plan.original_request} />
    <TextSection title="Confirmation" text={plan.confirmation_prompt} />
  </div>;
}

function TextSection({ title, text }: { title: string; text?: string }): React.JSX.Element | null {
  return text ? <section><h3>{title}</h3><p><PathText>{text}</PathText></p></section> : null;
}
function ListSection({ title, items }: { title: string; items?: string[] }): React.JSX.Element | null {
  return items?.length ? <section><h3>{title}</h3><ul>{items.map((item, index) => <li key={index}><PathText>{item}</PathText></li>)}</ul></section> : null;
}
