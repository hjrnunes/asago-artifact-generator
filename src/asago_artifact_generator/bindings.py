"""Authoring's binding declarations, re-exported from the ``asago-package`` member.

The closed vocabulary, the rules, the declaration-validation engine and authoring's
canonical paths and wording live in ``packages/asago-package``, beside the package
preparation that reads them. This module keeps the consumer's old import path working and
binds the same objects, so ``asago_artifact_generator.bindings`` and
``asago_package.bindings`` are one implementation.
"""

from __future__ import annotations

from asago_package.bindings import (
    AUTHORING_POLICY,
    BINDING_SPEC,
    CLOSED_TYPES,
    NORMALIZED_POLICY,
    BindingSpec,
    BindingValidationError,
    ConsumerPrefix,
    RuntimeBinding,
    authoring_message,
    binding_from_dict,
    canonical_binding_paths,
    check_bindings,
    find_stimulus_user_text_consumer_mismatches,
    named_record_facts,
    normalize_binding_declarations,
    paths_as_declared,
    root_source_refs,
    supplied_binding_values,
    supplied_values_as_declared,
    validate_bindings,
)
from asago_package.bindings import _binding_selector_type as _binding_selector_type
from asago_package.bindings import _first_fact_named as _first_fact_named

__all__ = [
    "AUTHORING_POLICY",
    "BINDING_SPEC",
    "BindingSpec",
    "BindingValidationError",
    "CLOSED_TYPES",
    "ConsumerPrefix",
    "NORMALIZED_POLICY",
    "RuntimeBinding",
    "authoring_message",
    "binding_from_dict",
    "canonical_binding_paths",
    "check_bindings",
    "find_stimulus_user_text_consumer_mismatches",
    "named_record_facts",
    "normalize_binding_declarations",
    "paths_as_declared",
    "root_source_refs",
    "supplied_binding_values",
    "supplied_values_as_declared",
    "validate_bindings",
]
