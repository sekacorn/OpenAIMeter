# Reconciliation

AIMeter separates calculated provider cost, provider-reported cost, invoiced cost, and reconciled cost. The Beta does not import provider invoices.

The reconciliation report compares available calculated, provider-reported, and invoiced values using a Decimal tolerance. Records with fewer than two values are marked `insufficient_evidence`; matching records are marked `matched`; material differences are marked `mismatch`. A mismatch is evidence of a difference between supplied values, not automatic evidence of fraud, overbilling, or an internal error.
