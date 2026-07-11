# Reconciliation

AIMeter separates calculated provider cost, provider-reported cost, invoiced cost, and reconciled cost. The alpha does not import provider invoices.

The reconciliation report compares available calculated, provider-reported, and invoiced values using a Decimal tolerance. Records with fewer than two values are marked `insufficient_evidence`; matching records are marked `matched`; material differences are marked `mismatch`.
