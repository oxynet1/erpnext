import frappe

def get_context(context):
	context['session_check'] = frappe.session.user