# -*- coding: utf-8 -*-
# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document

class MonthlyComplianceRecord(Document):
	pass


@frappe.whitelist()
def get_mntly_complin(contractor,fromdate,todate):
    x = frappe.db.sql("select skilled,unskilled,semiskilled,worked_days,emp_no from (select count(employee_category) as skilled  from `tabGate Pass` where  employee_category like 'Skilled' and contractor_name like '"+ contractor +"' and CAST(from_date AS DATE) between CAST('"+ fromdate +"' AS DATE) and CAST('"+ todate +"' AS DATE)) as z1, (select count(employee_category) as unskilled from `tabGate Pass` where  employee_category like 'Unskilled' and contractor_name like '"+ contractor +"' and CAST(from_date AS DATE) between CAST('"+ fromdate +"' AS DATE) and CAST('"+ todate +"' AS DATE)) as z2,	 (select count(employee_category) as semiskilled from `tabGate Pass` where  employee_category like 'Semiskilled' and contractor_name like '"+ contractor +"' and CAST(from_date AS DATE) between CAST('"+ fromdate +"' AS DATE) and CAST('"+ todate +"' AS DATE)) as z3,(select count(date) as worked_days from `tabGatePass Daily Entry` e, `tabGate Pass` g where g.name=e.parent and g.contractor_name like '"+ contractor +"' and CAST(date AS DATE) between CAST('"+ fromdate +"' AS DATE) and CAST('"+ todate +"' AS DATE)) as z4,(select count(name) as emp_no from `tabGate Pass`  where contractor_name like '"+ contractor +"' and CAST(from_date AS DATE) between CAST('"+ fromdate +"' AS DATE) and CAST('"+ todate +"' AS DATE)) as z5;")
    return x

