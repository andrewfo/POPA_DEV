"""Berth-request intake: land hand-driven request data into the data layer.

Requests originate as Adobe Sign forms, are harvested into a SharePoint list by
Power Automate, and exported as CSV. This package lands each row *raw* into
``intake_event`` (source ``form``) and normalizes conservatively. Reconciling
those requests against observed AIS occupancy is a LATER layer — intake only
captures the request faithfully here.
"""
