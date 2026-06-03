"""Berth-request intake: land hand-entered request data into the data layer.

Requests are entered **manually** by an operator (phone / email / walk-in) via
``manual.py``; the automated online-form feed (Adobe Sign -> SharePoint) has been
retired. Each entry lands *raw* into ``intake_event`` (deduped by content hash)
and projects a ``status='requested'`` reservation. Reconciling those requests
against observed AIS occupancy is a LATER layer.
"""
