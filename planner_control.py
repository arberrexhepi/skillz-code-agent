from __future__ import annotations

import re
from typing import Any, Dict, List


LOCAL_TAGGED_CONTROL_PROVIDER = "local"


def use_tagged_planner_control(provider: str) -> bool:
    return str(provider or "").strip().lower() == LOCAL_TAGGED_CONTROL_PROVIDER


def planner_intake_format_instructions(*, use_tags: bool) -> str:
    if not use_tags:
        return """
Return exactly one JSON object and nothing else.
Use strict JSON syntax: double-quoted keys and string values, no trailing commas, no comments.
Do NOT wrap the response in markdown fences or add any text before or after the JSON object.

Schema:
{
  \"thought\": \"brief reasoning\",
  \"action\": {
                    \"type\": \"ask_clarification\" | \"offer_discovery\" | \"present_plan\" | \"respond\",
    \"...\": \"action specific fields\"
  }
}

ask_clarification:
{
  \"thought\": \"...\",
  \"action\": {
    \"type\": \"ask_clarification\",
    \"question\": \"single concrete follow-up question\",
    \"reason\": \"why the answer matters\"
  }
}

present_plan:
{
  \"thought\": \"...\",
  \"action\": {
    \"type\": \"present_plan\",
    \"summary\": \"overall plan summary\",
    \"clarification_summary\": \"what was learned from the conversation\",
    \"assumptions\": [\"...\"],
    \"not_in_scope\": [\"explicit list of things this plan will NOT touch, change, or affect\"],
    \"next_steps_preview\": [\"...\"],
    \"confirmation_prompt\": \"short approval prompt\",
    \"goals\": [
      {
        \"goal_id\": \"goal-1\",
        \"title\": \"short label\",
        \"goal\": \"what the worker should accomplish\",
        \"reason\": \"why this goal is next\",
        \"depends_on\": [\"goal-0\"],
        \"preserve_context\": true,
                            \"parallelizable\": false,
                            \"estimated_scope\": \"read\" | \"write\" | \"mixed\" | \"validation\",
        \"delegation_notes\": [\"specific worker guidance\"],
                            \"success_signals\": [\"observable signs this goal is done\"],
                            \"relevant_fact_keys\": [\"durable_repo_fact_key\"]
      }
    ]
  }
}

offer_discovery:
{
  \"thought\": \"...\",
  \"action\": {
    \"type\": \"offer_discovery\",
    \"reason\": \"why codebase discovery is needed before planning\",
    \"prompt\": \"short explanation to the user\",
    \"recommended_mode\": \"quick\" | \"moderate\" | \"deep\"
  }
}

respond:
{
  \"thought\": \"...\",
  \"action\": {
    \"type\": \"respond\",
    \"message\": \"short message\"
  }
}
""".strip()
    return """
Return planner control using tags only. Do not return JSON.
Do not wrap in markdown fences. Do not add any prose before or after the tags.

Top-level shape:
<planner>
<thought>brief reasoning</thought>
<action>
<type>ask_clarification | offer_discovery | present_plan | respond</type>
...
</action>
</planner>

ask_clarification:
<planner>
<thought>...</thought>
<action>
<type>ask_clarification</type>
<question>single concrete follow-up question</question>
<reason>why the answer matters</reason>
</action>
</planner>

offer_discovery:
<planner>
<thought>...</thought>
<action>
<type>offer_discovery</type>
<reason>why codebase discovery is needed before planning</reason>
<prompt>short explanation to the user</prompt>
<recommended_mode>quick | moderate | deep</recommended_mode>
</action>
</planner>

respond:
<planner>
<thought>...</thought>
<action>
<type>respond</type>
<message>short message</message>
</action>
</planner>

present_plan:
<planner>
<thought>...</thought>
<action>
<type>present_plan</type>
<summary>overall plan summary</summary>
<clarification_summary>what was learned from the conversation</clarification_summary>
<assumption>one assumption</assumption>
<assumption>one assumption</assumption>
<not_in_scope>one explicit excluded area</not_in_scope>
<next_step_preview>one expected next step</next_step_preview>
<confirmation_prompt>short approval prompt</confirmation_prompt>
<goal>
<goal_id>goal-1</goal_id>
<title>short label</title>
<goal_text>what the worker should accomplish</goal_text>
<reason>why this goal is next</reason>
<depends_on>goal-0</depends_on>
<preserve_context>true|false</preserve_context>
<parallelizable>true|false</parallelizable>
<estimated_scope>read|write|mixed|validation</estimated_scope>
<delegation_note>specific worker guidance</delegation_note>
<success_signal>observable sign this goal is done</success_signal>
<relevant_fact_key>durable_repo_fact_key</relevant_fact_key>
</goal>
</action>
</planner>

Repeat <goal>, <assumption>, <not_in_scope>, <next_step_preview>, <depends_on>, <delegation_note>, <success_signal>, and <relevant_fact_key> as needed instead of using lists.
Rules for present_plan tag mode:
- present_plan is INVALID unless it contains at least one complete <goal>...</goal> block
- <goal_id>, <title>, <goal_text>, <reason>, <depends_on>, <preserve_context>, <parallelizable>, <estimated_scope>, <delegation_note>, <success_signal>, and <relevant_fact_key> must appear only inside a <goal> block
- never place goal fields directly under <action>
- after <confirmation_prompt>, emit one or more <goal> blocks immediately
""".strip()


def next_goal_guidance_instructions(*, use_tags: bool) -> str:
    if not use_tags:
        return """
You refine the handoff between one completed planner goal and the next one.
Return exactly one JSON object with:
{
  "commentary": "short specific commentary for the next goal",
  "preserve_context": true,
  "extra_notes": ["optional note"]
}
Keep commentary concrete and based on the completed result.
""".strip()
    return """
You refine the handoff between one completed planner goal and the next one.
Return tags only, no JSON, no markdown:
<guidance>
<commentary>short specific commentary for the next goal</commentary>
<preserve_context>true|false</preserve_context>
<extra_note>optional note</extra_note>
<extra_note>optional note</extra_note>
</guidance>
Keep commentary concrete and based on the completed result.
""".strip()


def final_summary_instructions(*, use_tags: bool) -> str:
    if not use_tags:
        return """
You are writing the planner's close-out after delegated goal execution.
Return exactly one JSON object:
{
  "summary": "short overall summary",
  "next_steps": ["specific next step", "specific next step"]
}
""".strip()
    return """
You are writing the planner's close-out after delegated goal execution.
Return tags only, no JSON, no markdown:
<final_summary>
<summary>short overall summary</summary>
<next_step>specific next step</next_step>
<next_step>specific next step</next_step>
</final_summary>
""".strip()


def parse_planner_intake_response(text: str) -> Dict[str, Any]:
    action_block = _extract_first_block(text, "action") or text
    action_type = _extract_first_value(action_block, "type")
    if not action_type:
        raise ValueError("Tagged planner response missing <type>.")
    payload: Dict[str, Any] = {
        "thought": _extract_first_value(text, "thought"),
        "action": {
            "type": action_type,
        },
    }
    action: Dict[str, Any] = payload["action"]
    if action_type == "ask_clarification":
        action["question"] = _extract_first_value(action_block, "question")
        action["reason"] = _extract_first_value(action_block, "reason")
        return payload
    if action_type == "offer_discovery":
        action["reason"] = _extract_first_value(action_block, "reason")
        action["prompt"] = _extract_first_value(action_block, "prompt")
        action["recommended_mode"] = _extract_first_value(action_block, "recommended_mode")
        return payload
    if action_type == "respond":
        action["message"] = _extract_first_value(action_block, "message")
        return payload
    if action_type != "present_plan":
        raise ValueError(f"Unsupported tagged planner action type: {action_type}")

    action["summary"] = _extract_first_value(action_block, "summary")
    action["clarification_summary"] = _extract_first_value(action_block, "clarification_summary")
    action["assumptions"] = _extract_all_values(action_block, "assumption")
    action["not_in_scope"] = _extract_all_values(action_block, "not_in_scope")
    action["next_steps_preview"] = _extract_all_values(action_block, "next_step_preview")
    action["confirmation_prompt"] = _extract_first_value(action_block, "confirmation_prompt")
    action["goals"] = [_parse_goal(block, index) for index, block in enumerate(_extract_all_blocks(action_block, "goal"), start=1)]
    if not action["goals"]:
        fallback_goal = _salvage_orphan_action_goal(action_block, action)
        if fallback_goal is not None:
            action["goals"] = [fallback_goal]
    return payload


def parse_next_goal_guidance_response(text: str) -> Dict[str, Any]:
    block = _extract_first_block(text, "guidance") or text
    preserve_context_raw = _extract_first_value(block, "preserve_context")
    payload: Dict[str, Any] = {
        "commentary": _extract_first_value(block, "commentary"),
        "extra_notes": _extract_all_values(block, "extra_note"),
    }
    if preserve_context_raw:
        payload["preserve_context"] = _parse_bool(preserve_context_raw)
    return payload


def parse_final_summary_response(text: str) -> Dict[str, Any]:
    block = _extract_first_block(text, "final_summary") or text
    return {
        "summary": _extract_first_value(block, "summary"),
        "next_steps": _extract_all_values(block, "next_step"),
    }


def _parse_goal(block: str, index: int) -> Dict[str, Any]:
    return {
        "goal_id": _extract_first_value(block, "goal_id") or f"goal-{index}",
        "title": _extract_first_value(block, "title"),
        "goal": _extract_first_value(block, "goal_text") or _extract_first_value(block, "goal"),
        "reason": _extract_first_value(block, "reason"),
        "depends_on": _extract_all_values(block, "depends_on"),
        "preserve_context": _parse_bool(_extract_first_value(block, "preserve_context")),
        "parallelizable": _parse_bool(_extract_first_value(block, "parallelizable")),
        "estimated_scope": _extract_first_value(block, "estimated_scope"),
        "delegation_notes": _extract_all_values(block, "delegation_note"),
        "success_signals": _extract_all_values(block, "success_signal"),
        "relevant_fact_keys": _extract_all_values(block, "relevant_fact_key"),
    }


def _salvage_orphan_action_goal(action_block: str, action: Dict[str, Any]) -> Dict[str, Any] | None:
    summary = str(action.get("summary") or "").strip()
    next_steps_preview = [str(item) for item in action.get("next_steps_preview") or [] if str(item).strip()]
    assumptions = [str(item) for item in action.get("assumptions") or [] if str(item).strip()]
    title = _extract_first_value(action_block, "title") or (next_steps_preview[0] if next_steps_preview else summary)
    goal_text = _extract_first_value(action_block, "goal_text") or _extract_first_value(action_block, "goal") or (next_steps_preview[0] if next_steps_preview else summary)
    reason = _extract_first_value(action_block, "reason") or (assumptions[0] if assumptions else summary)
    depends_on = [item for item in _extract_all_values(action_block, "depends_on") if item.lower() != "none"]
    delegation_notes = _extract_all_values(action_block, "delegation_note")
    success_signals = _extract_all_values(action_block, "success_signal")
    relevant_fact_keys = _extract_all_values(action_block, "relevant_fact_key")
    preserve_context_raw = _extract_first_value(action_block, "preserve_context")
    parallelizable_raw = _extract_first_value(action_block, "parallelizable")
    estimated_scope = _extract_first_value(action_block, "estimated_scope") or "mixed"

    if not title and not goal_text:
        return None

    return {
        "goal_id": "goal-1",
        "title": title or "Goal 1",
        "goal": goal_text or summary,
        "reason": reason or "No reason provided.",
        "depends_on": depends_on,
        "preserve_context": _parse_bool(preserve_context_raw) if preserve_context_raw else True,
        "parallelizable": _parse_bool(parallelizable_raw) if parallelizable_raw else False,
        "estimated_scope": estimated_scope,
        "delegation_notes": delegation_notes,
        "success_signals": success_signals,
        "relevant_fact_keys": relevant_fact_keys,
    }


def _extract_first_value(text: str, tag: str) -> str:
    match = re.search(_tag_pattern(tag), text, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return _clean_text(match.group(1))


def _extract_all_values(text: str, tag: str) -> List[str]:
    return [_clean_text(item) for item in re.findall(_tag_pattern(tag), text, re.DOTALL | re.IGNORECASE) if _clean_text(item)]


def _extract_first_block(text: str, tag: str) -> str:
    match = re.search(_tag_pattern(tag), text, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def _extract_all_blocks(text: str, tag: str) -> List[str]:
    return [item for item in re.findall(_tag_pattern(tag), text, re.DOTALL | re.IGNORECASE)]


def _tag_pattern(tag: str) -> str:
    safe_tag = re.escape(tag)
    return rf"<{safe_tag}>\s*(.*?)\s*</{safe_tag}>"


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _parse_bool(text: str) -> bool:
    return str(text or "").strip().lower() in {"1", "true", "yes", "y"}
