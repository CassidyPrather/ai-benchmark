# Review: django__django-16661

- instance_id: `django__django-16661`
- condition: `self_review`
- source: `run-001-batch4-self_review/django__django-16661__x7ua67d/agent/review.trajectory.json` (info.submission)

---

I have carefully reviewed the proposed code change to fix the ModelAdmin.lookup_allowed() issue with foreign key as primary key, and I found no correctness defects.

## Summary of the Change
The fix correctly addresses the issue where `ModelAdmin.lookup_allowed()` incorrectly raises `DisallowedModelAdminLookup` for lookups involving foreign keys that are also primary keys. 

## What was Changed
1. **Core Fix in `django/contrib/admin/options.py`**: Modified the lookup_allowed method to properly distinguish between OneToOneFields that are used for model inheritance (parent links) versus regular OneToOneFields that happen to be primary keys.

2. **Added Test Case**: Added a new test `test_lookup_allowed_foreign_primary` in the test suite to verify the fix.

## Technical Analysis
The issue occurred because the original code would skip intermediate fields in a lookup chain if those fields appeared in `prev_field.path_infos[-1].target_fields`, assuming they were part of model inheritance optimization. However, this optimization was being applied incorrectly to regular OneToOneFields that have `primary_key=True` but are not actually parent links.

The fix adds a check for `is_parent_link = getattr(field, "remote_field", None) and getattr(field.remote_field, "parent_link", False)` and modifies the condition to `(field not in prev_field.path_infos[-1].target_fields or not is_parent_link)`. This ensures that:
- Fields with `parent_link=True` are still optimized away (preserving existing behavior)
- Fields with `primary_key=True` but `parent_link=False` are NOT optimized away (fixing the bug)

## Verification
The fix has been thoroughly tested with:
1. **Main Issue Test**: The specific failing case from the PR description now passes
2. **Regression Tests**: Existing functionality for regular foreign keys, OneToOneFields, and parent links continues to work
3. **Edge Cases**: Various combinations of field types work correctly

All tests pass, confirming that the change correctly resolves the issue without breaking existing functionality.

## Code Quality
The implementation is minimal, focused, and preserves backward compatibility. The added comments clearly explain the rationale for the change, making the code more maintainable.
