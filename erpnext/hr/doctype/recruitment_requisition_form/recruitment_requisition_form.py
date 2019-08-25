# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document

class RecruitmentRequisitionForm(Document):
	pass


@frappe.whitelist(allow_guest=True)
def get_job_opening():
    item_detls = {}
    data=[]
    a= frappe.db.sql("SELECT name,desig,position,dept,no_of_positions,due_date ,CAST(NOW() As DATE) FROM `tabRecruitment Requisition Form` where due_date >= CAST(NOW() As DATE) and workflow_state like 'Approved by Controlling';", as_dict=1)
    for d in a:
      item_detls[d.da_vehclno] = d
      data.append(d)
    return data
