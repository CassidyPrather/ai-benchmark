# Review: django__django-15127

- instance_id: `django__django-15127`
- condition: `self_review`
- source: `run-001-batch3-self_review/django__django-15127__49MUgzS/agent/review.trajectory.json` (info.submission)

---

## Correctness Defects Found

### 1. Breaking Change to Existing Workaround

**File:** `django/contrib/messages/storage/base.py` (and related `tests/messages_tests/base.py`)

**What:** The change removes the module-level `LEVEL_TAGS` variable that was previously defined as:
```python
LEVEL_TAGS = utils.get_level_tags()
```

**Why it's a defect:** 
1. There exists an existing workaround in the Django test suite (`tests/messages_tests/base.py`) that depends on this module-level variable. The `override_settings_tags` class directly manipulates `base.LEVEL_TAGS` to work around the exact issue this change is trying to fix.

2. Any third-party code that might be accessing `django.contrib.messages.storage.base.LEVEL_TAGS` directly will break with an `AttributeError`.

3. While the change correctly fixes the core issue (making `level_tag` property respond to `@override_settings`), it does so in a way that breaks backward compatibility.

**How it manifests:**
- The existing test helper `override_settings_tags` will raise `AttributeError: module 'django.contrib.messages.storage.base' has no attribute 'LEVEL_TAGS'`
- Any external code that was manually updating `LEVEL_TAGS` for testing purposes will break

**Recommendation:**
Instead of removing `LEVEL_TAGS` entirely, consider keeping it as a module-level variable but making it a function or property that returns fresh values from `utils.get_level_tags()` when accessed. This would maintain backward compatibility while fixing the core issue.

For example:
```python
def _get_level_tags():
    return utils.get_level_tags()

# Keep the variable name for backward compatibility
LEVEL_TAGS = _get_level_tags

# Then in the Message class:
@property
def level_tag(self):
    return _get_level_tags().get(self.level, '')
```

Or alternatively, the change is correct in functionality but should be marked as a breaking change that requires updating the test suite workaround.
