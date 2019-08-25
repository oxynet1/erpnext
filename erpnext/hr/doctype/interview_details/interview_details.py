# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document

class InterviewDetails(Document):
	pass


@frappe.whitelist()
def update_selection_status(status,candidate):
        frappe.db.sql("update `tabCandidate Details` set selection_status= '" + status + "' where name like '" + candidate + "';")




@frappe.whitelist(allow_guest=True)
def insert_cndadte_dtls(email,first_name,last_name,username,gender,mobile_no,phone,new_password):
        frappe.db.sql("INSERT INTO `tabUser`( email ,first_name ,last_name,username, gender ,mobile_no , phone , new_password )\
                       VALUES('" + email + "' ,'" + first_name + "', '" + last_name + "','" + username + "','" + gender + "','" + mobile_no + "', '" + phone + "','" + new_password + "');")


@frappe.whitelist(allow_guest=True)
def insert_cndadte_dtls1(email,first_name):
        frappe.db.sql("INSERT INTO `tabUser`( email ,first_name,)\
                       VALUES('" + email + "' ,'" + first_name + "');")


@frappe.whitelist(allow_guest=True)
def get_job_opening():
    item_detls = {}
    data=[]
    a= frappe.db.sql("SELECT name,desig,position,dept,no_of_positions,due_date ,CAST(NOW() As DATE) FROM `tabRecruitment Requisition Form` where due_date >= CAST(NOW() As DATE) and workflow_state like 'Approved by Controlling';", as_dict=1)
    for d in a:
      item_detls[d.da_vehclno] = d
      data.append(d)
    return data



