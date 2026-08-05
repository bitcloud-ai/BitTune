package autopilot.authz

import rego.v1

default decision := {
    "allow": false,
    "reason_code": "POLICY_DENIED",
    "requirements": {"human_approval": false},
}

decision := {
    "allow": reason_code == "ALLOW",
    "reason_code": reason_code,
    "requirements": {"human_approval": input.tool.risk_level == "L2"},
}

reason_code := "L3_FORBIDDEN" if {
    input.tool.risk_level == "L3"
} else := "TOOL_CONTEXT_DENIED" if {
    not base_context_allowed
} else := "ALLOW" if {
    input.purpose == "visibility"
} else := "BUDGET_EXCEEDED" if {
    budget_exceeded
} else := "APPROVAL_REQUIRED" if {
    input.tool.risk_level == "L2"
    not approved
} else := "APPROVAL_IDENTITY_DENIED" if {
    input.tool.risk_level == "L2"
    not approval_identity_allowed
} else := "APPROVAL_MISMATCH" if {
    input.tool.risk_level == "L2"
    not approval_binding_matches
} else := "APPROVAL_EXPIRED" if {
    input.tool.risk_level == "L2"
    approval_expired
} else := "ALLOW" if {
    input.tool.risk_level in {"L0", "L1", "L2"}
}

base_context_allowed if {
    input.phase in input.tool.allowed_phases
    input.subject.role in input.tool.allowed_roles
    input.tool.environment_supported
    input.tool.provider_enabled
    input.tool.feature_flags_enabled
}

budget_exceeded if {
    input.budget.requested.max_duration_seconds > input.budget.ceiling.max_duration_seconds
}

budget_exceeded if {
    input.budget.requested.max_requests > input.budget.ceiling.max_requests
}

budget_exceeded if {
    input.budget.requested.max_input_tokens > input.budget.ceiling.max_input_tokens
}

budget_exceeded if {
    input.budget.requested.max_output_tokens > input.budget.ceiling.max_output_tokens
}

budget_exceeded if {
    input.budget.requested.max_disk_growth_bytes > input.budget.ceiling.max_disk_growth_bytes
}

approved if {
    input.approval.decision == "approved"
}

approval_identity_allowed if {
    input.approval.requester.kind == "human"
    input.approval.decided_by.kind == "human"
    input.approval.decided_by.role == "admin"
    input.approval.requester.user_id != input.approval.decided_by.user_id
}

approval_binding_matches if {
    input.approval.experiment_id == input.plan.experiment_id
    input.approval.plan_id == input.plan.plan_id
    input.approval.plan_hash == input.plan.plan_hash
    input.approval.action == input.tool.name
}

approval_expired if {
    time.parse_rfc3339_ns(input.approval.expires_at) <= time.parse_rfc3339_ns(input.current_time)
}
