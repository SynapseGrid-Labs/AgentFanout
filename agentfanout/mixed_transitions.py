"""Host-authorized routing transactions; no model or lifecycle effects."""
from copy import deepcopy
import uuid

from .mixed_contracts import MixedContractError, canonical_digest, validate_contract, binding_for


def validate_history(contract, history):
    validate_contract(contract)
    if contract['schema'] != 'MixedRunContractV2' or not isinstance(history, list) or len(history) > 4096:
        raise MixedContractError('transition_history_unsupported')
    current = {(a['assignment_id'], role): deepcopy(a[role]) for a in contract['assignments'] for role in ('worker', 'validator')}
    counts, ids = {}, set()
    for index, record in enumerate(history):
        keys = {'schema','transition_id','sequence','assignment_id','role','from_binding_sha256','to_binding','reason','proof_sha256'}
        if not isinstance(record, dict) or set(record) != keys or record['schema'] != 'MixedTransitionV1' or type(record['sequence']) is not int or record['sequence'] != index + 1:
            raise MixedContractError('transition_history_invalid')
        try:
            if str(uuid.UUID(record['transition_id'])) != record['transition_id']: raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MixedContractError('transition_id_invalid') from None
        if record['transition_id'] in ids:
            raise MixedContractError('duplicate_transition_id')
        ids.add(record['transition_id'])
        key = (record['assignment_id'], record['role'])
        if key not in current or record['from_binding_sha256'] != canonical_digest(current[key]):
            raise MixedContractError('transition_chain_conflict')
        edge = next((e for e in contract['allowed_transitions'] if all(e[k] == record[k] for k in ('assignment_id','role','from_binding_sha256','to_binding','reason'))), None)
        if edge is None:
            raise MixedContractError('transition_not_authorized')
        digest = canonical_digest(edge)
        counts[digest] = counts.get(digest, 0) + 1
        if counts[digest] > edge['max_count']:
            raise MixedContractError('transition_limit_exceeded')
        proof = record['proof_sha256']
        if not isinstance(proof, str) or len(proof) != 64 or any(c not in '0123456789abcdef' for c in proof):
            raise MixedContractError('transition_proof_missing')
        current[key] = deepcopy(record['to_binding'])
    return current


def effective_contract(contract, history):
    current = validate_history(contract, history)
    view = deepcopy(contract)
    for a in view['assignments']:
        for role in ('worker','validator'):
            a[role] = current[a['assignment_id'],role]
    view.pop('contract_sha256')
    view['contract_sha256'] = canonical_digest(view)
    return validate_contract(view)


def family(state, root):
    found = {root}
    while True:
        added = {t['task_id'] for t in state['tasks'] if t.get('parent_task_id') in found}
        if added <= found: return found
        found |= added


def apply_record(state, contract, history, record):
    validate_history(contract, [*history,record])
    if state['status'] == 'complete':
        raise MixedContractError('transition_run_already_complete')
    affected = family(state, record['assignment_id'])
    # Even superseded launch handles must have been consumed. Host proof alone
    # cannot erase an unresolved handle or outstanding numeric reservation.
    if any(r['task_id'] in affected and r['status'] != 'consumed' for r in state['launch_records'].values()):
        raise MixedContractError('transition_launch_effects_unresolved')
    if float(state['usage'].get('reserved_cost_usd',0)) != 0:
        raise MixedContractError('transition_accounting_unreconciled')
    tasks = [t for t in state['tasks'] if t['task_id'] in affected]
    if record['role'] == 'worker':
        if any(t['status'] not in {'pending','ready','failed','blocked'} for t in tasks):
            raise MixedContractError('transition_worker_result_or_effect_exists')
        b = record['to_binding']
        for task in tasks:
            task['provider_model'].update(provider=b['provider'],selector=b['model_selector'],resolved_model=b['resolved_model'])
            task['reasoning_effort'] = b['reasoning_effort']
            task['recommended_worker_handle'] = None
            if task['status'] in {'failed','blocked'}:
                task['status'] = 'pending'
        if state['status'] == 'blocked':
            state['status'] = 'replanning'
    elif any(state['validation_status'].get(t['task_id']) == 'passed' for t in tasks):
        raise MixedContractError('transition_validation_already_accepted')
    history.append(deepcopy(record))


def validate_reconciliation(proof, state, contract, ledger_tip):
    keys = {'schema','run_id','contract_sha256','state_revision','ledger_tip','effects_absent','accounting_reconciled','ownership','cancellation'}
    if not isinstance(proof,dict) or set(proof) != keys or proof['schema'] != 'MixedTransitionReconciliationV1':
        raise MixedContractError('transition_reconciliation_required')
    if (proof['run_id'] != state['run_id'] or proof['contract_sha256'] != contract['contract_sha256']
            or type(proof['state_revision']) is not int or proof['state_revision'] != state['revision'] or proof['ledger_tip'] != ledger_tip):
        raise MixedContractError('transition_reconciliation_stale')
    if proof['effects_absent'] is not True or proof['accounting_reconciled'] is not True or proof['ownership'] != 'certain' or proof['cancellation'] not in {'not_requested','confirmed'}:
        raise MixedContractError('transition_effects_or_accounting_unknown')
