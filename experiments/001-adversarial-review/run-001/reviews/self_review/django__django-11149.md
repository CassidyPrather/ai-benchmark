# Review: django__django-11149

- instance_id: `django__django-11149`
- condition: `self_review`
- source: `run-001-batch1-self_review/django__django-11149__q2r5yFc/agent/review.trajectory.json` (info.submission)

---

After carefully reviewing the proposed change, I found no correctness defects. The patch correctly addresses the security issue where users with only view permissions could perform add/delete operations on ManyToMany relationships through inlines.

The fix:
1. Modifies `has_add_permission` and `has_delete_permission` for auto-created models to require `change` permission on the target model
2. Preserves existing behavior for `has_change_permission` and `has_view_permission` 
3. Correctly identifies the target model in ManyToMany relationships
4. Maintains backward compatibility for users with appropriate permissions
5. Only affects auto-created intermediate models, leaving regular models unchanged

Testing confirms the fix works as intended:
- Users with view-only permissions can view/edit relationships but cannot add/delete them
- Users with change permissions can perform all operations
- The security bug is resolved without breaking legitimate functionality

No correctness defects were identified in this change.
