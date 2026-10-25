"""No admin for the claims app.

Claim and ClaimHistory must never be registered as editable: an edited claim would bypass
claims.services, its validation and the reprojection, and the models refuse such writes
anyway (claims.models.ClaimWriteError). The moderation admin (Task 11) shows claims
read-only, on the canonical models' pages, and writes through
claims.services.moderation_write / moderation_delete only.
"""
