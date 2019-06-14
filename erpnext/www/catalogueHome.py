import frappe

def get_context(context):
    context['catalogues'] = frappe.db.get_all("Catalogue")