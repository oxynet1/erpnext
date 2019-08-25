import frappe

def get_context(context):
	context['logged_in_user'] = frappe.session.user
