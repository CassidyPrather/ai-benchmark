# Review: django__django-13112

- instance_id: `django__django-13112`
- condition: `self_review`
- source: `run-001-batch1-self_review/django__django-13112__KMUcyBT/agent/review.trajectory.json` (info.submission)

---

# Code Review: Fix for ForeignKey with mixed-case app name

## Summary

The proposed fix attempts to address a regression in Django 3.1 where ForeignKey fields in models belonging to apps with mixed-case names cause makemigrations to crash. The error occurs because of case-sensitive app label handling during lazy reference resolution.

## Correctness Defects

### 1. Incomplete Application of Fix in `related.py`

**Location**: `django/db/models/fields/related.py`

**Defect**: The patch file indicates the intent to change how app_label is handled in related_query_name formatting within ForeignObject.contribute_to_class, specifically:
```diff
- 'app_label': cls._meta.app_label.lower(),
+ 'app_label': cls._meta.app_label,
```

However, **the change was never applied** to the actual source file. The current code still contains:
```python
'app_label': cls._meta.app_label.lower(),
```

**Why it's a defect**: This leaves the core issue unresolved. During related query name formatting, the app_label is still being converted to lowercase, which contributes to the case mismatch that causes the reported error.

**Manifestation**: The incomplete fix will still result in the original error where `'dj_reglogin.category'` is generated instead of `'DJ_RegLogin.category'` for lazy references.

### 2. Incomplete Pattern Matching in `related.py` ManyToManyField

**Location**: `django/db/models/fields/related.py` (ManyToManyField class)

**Defect**: While the ForeignObject class had its related_query_name formatting addressed (intended but not actually applied), the ManyToManyField class also contains a similar pattern in its contribute_to_class method but was not mentioned in the fix.

**Why it's a defect**: ManyToManyField also formats related names with lowercase app_label, which could potentially suffer from the same case sensitivity issues.

**Manifestation**: If ManyToManyFields are used with mixed-case app names, they may exhibit similar case-sensitive reference errors.

## Analysis

The issue stems from a regression in Django 3.1 where app label case handling was changed inconsistently. While the Options class correctly preserves app label case (after the fix in options.py), the related field formatting in ForeignObject and ManyToManyField classes continues to normalize app labels to lowercase.

The core problem occurs during:
1. String formatting of constraint/index names (already addressed)
2. String formatting of related query names (incompletely addressed)  
3. String formatting in ManyToManyField (not addressed)
4. Lazy reference construction and resolution

## Recommendation

To properly fix this issue, the following changes need to be made:

1. Apply the intended change in `related.py` for ForeignObject's related_query_name formatting
2. Consider whether ManyToManyField also needs similar treatment
3. Verify that the fix resolves the case sensitivity issue in all field types consistently

The fix should be comprehensive and ensure app label case consistency across all string formatting operations that affect model references.
