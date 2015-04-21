# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, json

from frappe.utils import flt, nowdate, get_datetime, getdate, date_diff, cint
from frappe import _
from frappe.model.document import Document
from erpnext.manufacturing.doctype.bom.bom import validate_bom_no
from dateutil.relativedelta import relativedelta

class OverProductionError(frappe.ValidationError): pass
class StockOverProductionError(frappe.ValidationError): pass
class OperationTooLongError(frappe.ValidationError): pass

from erpnext.manufacturing.doctype.workstation.workstation import WorkstationHolidayError, NotInWorkingHoursError
from erpnext.projects.doctype.time_log.time_log import OverlapError

form_grid_templates = {
	"operations": "templates/form_grid/production_order_grid.html"
}

class ProductionOrder(Document):
	def validate(self):
		if self.docstatus == 0:
			self.status = "Draft"

		from erpnext.utilities import validate_status
		validate_status(self.status, ["Draft", "Submitted", "Stopped",
			"In Process", "Completed", "Cancelled"])

		if self.bom_no:
			validate_bom_no(self.production_item, self.bom_no)

		self.validate_sales_order()
		self.validate_warehouse()
		self.calculate_operating_cost()
		self.validate_delivery_date()

		from erpnext.utilities.transaction_base import validate_uom_is_integer
		validate_uom_is_integer(self, "stock_uom", ["qty", "produced_qty"])

	def validate_sales_order(self):
		if self.sales_order:
			so = frappe.db.sql("""select name, delivery_date from `tabSales Order`
				where name=%s and docstatus = 1""", self.sales_order, as_dict=1)

			if len(so):
				if not self.expected_delivery_date:
					self.expected_delivery_date = so[0].delivery_date

				self.validate_production_order_against_so()
			else:
				frappe.throw(_("Sales Order {0} is not valid").format(self.sales_order))

	def validate_warehouse(self):
		from erpnext.stock.utils import validate_warehouse_company

		for w in [self.fg_warehouse, self.wip_warehouse]:
			validate_warehouse_company(w, self.company)

	def calculate_operating_cost(self):
		self.planned_operating_cost, self.actual_operating_cost = 0.0, 0.0
		for d in self.get("operations"):
			d.actual_operating_cost = flt(d.hour_rate) * (flt(d.actual_operation_time) / 60.0)

			self.planned_operating_cost += flt(d.planned_operating_cost)
			self.actual_operating_cost += flt(d.actual_operating_cost)

		variable_cost = self.actual_operating_cost if self.actual_operating_cost else self.planned_operating_cost
		self.total_operating_cost = flt(self.additional_operating_cost) + flt(variable_cost)

	def validate_production_order_against_so(self):
		# already ordered qty
		ordered_qty_against_so = frappe.db.sql("""select sum(qty) from `tabProduction Order`
			where production_item = %s and sales_order = %s and docstatus < 2 and name != %s""",
			(self.production_item, self.sales_order, self.name))[0][0]

		total_qty = flt(ordered_qty_against_so) + flt(self.qty)

		# get qty from Sales Order Item table
		so_item_qty = frappe.db.sql("""select sum(qty) from `tabSales Order Item`
			where parent = %s and item_code = %s""",
			(self.sales_order, self.production_item))[0][0]
		# get qty from Packing Item table
		dnpi_qty = frappe.db.sql("""select sum(qty) from `tabPacked Item`
			where parent = %s and parenttype = 'Sales Order' and item_code = %s""",
			(self.sales_order, self.production_item))[0][0]
		# total qty in SO
		so_qty = flt(so_item_qty) + flt(dnpi_qty)

		if total_qty > so_qty:
			frappe.throw(_("Cannot produce more Item {0} than Sales Order quantity {1}").format(self.production_item,
				so_qty), OverProductionError)

	def stop_unstop(self, status):
		""" Called from client side on Stop/Unstop event"""
		self.update_status(status)
		qty = (flt(self.qty)-flt(self.produced_qty)) * ((status == 'Stopped') and -1 or 1)
		self.update_planned_qty(qty)
		frappe.msgprint(_("Production Order status is {0}").format(status))


	def update_status(self, status=None):
		if not status:
			status = self.status

		if status != 'Stopped':
			stock_entries = frappe._dict(frappe.db.sql("""select purpose, sum(fg_completed_qty)
				from `tabStock Entry` where production_order=%s and docstatus=1
				group by purpose""", self.name))

			status = "Submitted"
			if stock_entries:
				status = "In Process"
				produced_qty = stock_entries.get("Manufacture")
				if flt(produced_qty) == flt(self.qty):
					status = "Completed"

		if status != self.status:
			self.db_set("status", status)

	def update_production_order_qty(self):
		"""Update **Manufactured Qty** and **Material Transferred for Qty** in Production Order
			based on Stock Entry"""

		for purpose, fieldname in (("Manufacture", "produced_qty"),
			("Material Transfer for Manufacture", "material_transferred_for_manufacturing")):
			qty = flt(frappe.db.sql("""select sum(fg_completed_qty)
				from `tabStock Entry` where production_order=%s and docstatus=1
				and purpose=%s""", (self.name, purpose))[0][0])

			if qty > self.qty:
				frappe.throw(_("{0} ({1}) cannot be greater than planned quanitity ({2}) in Production Order {3}").format(\
					self.meta.get_label(fieldname), qty, self.qty, self.name), StockOverProductionError)

			self.db_set(fieldname, qty)

	def on_submit(self):
		if not self.wip_warehouse:
			frappe.throw(_("Work-in-Progress Warehouse is required before Submit"))
		if not self.fg_warehouse:
			frappe.throw(_("For Warehouse is required before Submit"))
		frappe.db.set(self,'status', 'Submitted')
		self.make_time_logs()
		self.update_planned_qty(self.qty)


	def on_cancel(self):
		# Check whether any stock entry exists against this Production Order
		stock_entry = frappe.db.sql("""select name from `tabStock Entry`
			where production_order = %s and docstatus = 1""", self.name)
		if stock_entry:
			frappe.throw(_("Cannot cancel because submitted Stock Entry {0} exists").format(stock_entry[0][0]))

		frappe.db.set(self,'status', 'Cancelled')
		self.update_planned_qty(-self.qty)
		self.delete_time_logs()

	def update_planned_qty(self, qty):
		"""update planned qty in bin"""
		args = {
			"item_code": self.production_item,
			"warehouse": self.fg_warehouse,
			"posting_date": nowdate(),
			"planned_qty": flt(qty)
		}
		from erpnext.stock.utils import update_bin
		update_bin(args)

	def set_production_order_operations(self):
		"""Fetch operations from BOM and set in 'Production Order'"""

		self.set('operations', [])

		operations = frappe.db.sql("""select operation, description, workstation, idx,
			hour_rate, time_in_mins, operating_cost as "planned_operating_cost", "Pending" as status
			from `tabBOM Operation` where parent = %s order by idx""", self.bom_no, as_dict=1)

		self.set('operations', operations)

		self.calculate_operating_cost()


	def get_holidays(self, workstation):
		holiday_list = frappe.db.get_value("Workstation", workstation, "holiday_list")

		holidays = {}

		if holiday_list not in holidays:
			holiday_list_days = [getdate(d[0]) for d in frappe.get_all("Holiday", fields=["holiday_date"],
				filters={"parent": holiday_list}, order_by="holiday_date", limit_page_length=0, as_list=1)]

			holidays[holiday_list] = holiday_list_days

		return holidays[holiday_list]

	def make_time_logs(self):
		"""Capacity Planning. Plan time logs based on earliest availablity of workstation after
			Planned Start Date. Time logs will be created and remain in Draft mode and must be submitted
			before manufacturing entry can be made."""

		if not self.operations:
			return

		time_logs = []
		plan_days = frappe.db.get_single_value("Manufacturing Settings", "capacity_planning_for_days") or 30

		for i, d in enumerate(self.operations):
			self.set_operation_start_end_time(i, d)

			time_log = make_time_log(self.name, d.operation, d.planned_start_time, d.planned_end_time,
				flt(self.qty) - flt(d.completed_qty), self.project_name, d.workstation, operation_id=d.name)

			self.check_operation_fits_in_working_hours(d)

			original_start_time = time_log.from_time
			while True:
				_from_time = time_log.from_time
				try:
					time_log.save()
					break
				except WorkstationHolidayError:
					time_log.move_to_next_day()
				except NotInWorkingHoursError:
					time_log.move_to_next_working_slot()
				except OverlapError:
					time_log.move_to_next_non_overlapping_slot()

				# reset end time
				time_log.to_time = get_datetime(time_log.from_time) + relativedelta(minutes=d.time_in_mins)

				if date_diff(time_log.from_time, original_start_time) > plan_days:
					frappe.msgprint(_("Unable to find Time Slot in the next {0} days for Operation {1}").format(plan_days, d.operation))
					break

				if _from_time == time_log.from_time:
					frappe.throw("Capacity Planning Error")

			d.planned_start_time = time_log.from_time
			d.planned_end_time = time_log.to_time
			d.db_update()

			if time_log.name:
				time_logs.append(time_log.name)

		self.planned_end_date = self.operations[-1].planned_end_time

		if time_logs:
			frappe.local.message_log = []
			frappe.msgprint(_("Time Logs created:") + "\n" + "\n".join(time_logs))

	def set_operation_start_end_time(self, i, d):
		"""Set start and end time for given operation. If first operation, set start as
		`planned_start_date`, else add time diff to end time of earlier operation."""
		if self.planned_start_date:
			if i==0:
				# first operation at planned_start date
				d.planned_start_time = self.planned_start_date
			else:
				d.planned_start_time = get_datetime(self.operations[i-1].planned_end_time)\
					+ self.get_mins_between_operations()

			d.planned_end_time = get_datetime(d.planned_start_time) + relativedelta(minutes = d.time_in_mins)

			if d.planned_start_time == d.planned_end_time:
				frappe.throw(_("Capacity Planning Error"))

	def get_mins_between_operations(self):
		if not hasattr(self, "_mins_between_operations"):
			self._mins_between_operations = cint(frappe.db.get_single_value("Manufacturing Settings",
				"mins_between_operations")) or 10
		return relativedelta(minutes=self._mins_between_operations)

	def check_operation_fits_in_working_hours(self, d):
		"""Raises expection if operation is longer than working hours in the given workstation."""
		from erpnext.manufacturing.doctype.workstation.workstation import check_if_within_operating_hours
		check_if_within_operating_hours(d.workstation, d.operation, d.planned_start_time, d.planned_end_time)

	def update_operation_status(self):
		for d in self.get("operations"):
			if not d.completed_qty:
				d.status = "Pending"
			elif flt(d.completed_qty) < flt(self.qty):
				d.status = "Work in Progress"
			elif flt(d.completed_qty) == flt(self.qty):
				d.status = "Completed"
			else:
				frappe.throw(_("Completed Qty can not be greater than 'Qty to Manufacture'"))

	def set_actual_dates(self):
		if self.get("operations"):
			actual_date = frappe.db.sql("""select min(actual_start_time) as start_date, max(actual_end_time) as end_date from `tabProduction Order Operation`
				where parent = %s and docstatus=1""", self.name, as_dict=1)[0]
			self.actual_start_date = actual_date.start_date
			self.actual_end_date = actual_date.end_date
		else:
			self.actual_start_date = None
			self.actual_end_date = None

	def validate_delivery_date(self):
		if self.docstatus==1:
			if self.planned_end_date and self.expected_delivery_date \
				and getdate(self.expected_delivery_date) < getdate(self.planned_end_date):
					frappe.msgprint(_("Production might not be able to finish by the Expected Delivery Date."))

	def delete_time_logs(self):
		for time_log in frappe.get_all("Time Log", ["name"], {"production_order": self.name}):
			frappe.delete_doc("Time Log", time_log.name)

@frappe.whitelist()
def get_item_details(item):
	res = frappe.db.sql("""select stock_uom, description
		from `tabItem` where (ifnull(end_of_life, "0000-00-00")="0000-00-00" or end_of_life > now())
		and name=%s""", item, as_dict=1)

	if not res:
		return {}

	res = res[0]
	res["bom_no"] = frappe.db.get_value("BOM", filters={"item": item, "is_default": 1})
	return res

@frappe.whitelist()
def make_stock_entry(production_order_id, purpose, qty=None):
	production_order = frappe.get_doc("Production Order", production_order_id)

	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.purpose = purpose
	stock_entry.production_order = production_order_id
	stock_entry.company = production_order.company
	stock_entry.from_bom = 1
	stock_entry.bom_no = production_order.bom_no
	stock_entry.additional_operating_cost = production_order.additional_operating_cost
	stock_entry.use_multi_level_bom = production_order.use_multi_level_bom
	stock_entry.fg_completed_qty = qty or (flt(production_order.qty) - flt(production_order.produced_qty))

	if purpose=="Material Transfer for Manufacture":
		stock_entry.to_warehouse = production_order.wip_warehouse
	else:
		stock_entry.from_warehouse = production_order.wip_warehouse
		stock_entry.to_warehouse = production_order.fg_warehouse

	stock_entry.get_items()
	return stock_entry.as_dict()

@frappe.whitelist()
def get_events(start, end, filters=None):
	from frappe.desk.reportview import build_match_conditions
	if not frappe.has_permission("Production Order"):
		frappe.msgprint(_("No Permission"), raise_exception=1)

	conditions = build_match_conditions("Production Order")
	conditions = conditions and (" and " + conditions) or ""
	if filters:
		filters = json.loads(filters)
		for key in filters:
			if filters[key]:
				conditions += " and " + key + ' = "' + filters[key].replace('"', '\"') + '"'

	data = frappe.db.sql("""select name, production_item, planned_start_date, planned_end_date
		from `tabProduction Order`
		where ((ifnull(planned_start_date, '0000-00-00')!= '0000-00-00') \
				and (planned_start_date between %(start)s and %(end)s) \
			or ((ifnull(planned_start_date, '0000-00-00')!= '0000-00-00') \
				and planned_end_date between %(start)s and %(end)s)) {conditions}
		""".format(conditions=conditions), {
			"start": start,
			"end": end
		}, as_dict=True, update={"allDay": 0})
	return data

@frappe.whitelist()
def make_time_log(name, operation, from_time, to_time, qty=None,  project=None, workstation=None, operation_id=None):
	time_log =  frappe.new_doc("Time Log")
	time_log.for_manufacturing = 1
	time_log.from_time = from_time
	time_log.to_time = to_time
	time_log.production_order = name
	time_log.project = project
	time_log.operation_id = operation_id
	time_log.operation = operation
	time_log.workstation= workstation
	time_log.activity_type= "Manufacturing"
	time_log.completed_qty = flt(qty)

	if from_time and to_time :
		time_log.calculate_total_hours()
	return time_log
