# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe
from frappe.utils import cint, cstr
from frappe import throw, _
from frappe.utils.nestedset import NestedSet, get_ancestors_of, get_descendants_of

class RootNotEditable(frappe.ValidationError): pass
class BalanceMismatchError(frappe.ValidationError): pass

class Account(NestedSet):
	nsm_parent_field = 'parent_account'
	def on_update(self):
		if frappe.local.flags.ignore_on_update:
			return
		else:
			super(Account, self).on_update()

	def onload(self):
		frozen_accounts_modifier = frappe.db.get_value("Accounts Settings", "Accounts Settings",
			"frozen_accounts_modifier")
		if not frozen_accounts_modifier or frozen_accounts_modifier in frappe.get_roles():
			self.set_onload("can_freeze_account", True)

	def autoname(self):
		from erpnext.accounts.utils import get_autoname_with_number
		self.name = get_autoname_with_number(self.account_number, self.account_name, None, self.company)

	def validate(self):
		from erpnext.accounts.utils import validate_field_number
		if frappe.local.flags.allow_unverified_charts:
			return
		self.validate_parent()
		self.validate_root_details()
		validate_field_number("Account", self.name, self.account_number, self.company, "account_number")
		self.validate_group_or_ledger()
		self.set_root_and_report_type()
		self.validate_mandatory()
		self.validate_frozen_accounts_modifier()
		self.validate_balance_must_be_debit_or_credit()
		self.validate_account_currency()
		self.validate_root_company_and_sync_account_to_children()

	def validate_parent(self):
		"""Fetch Parent Details and validate parent account"""
		if self.parent_account:
			par = frappe.db.get_value("Account", self.parent_account,
				["name", "is_group", "company"], as_dict=1)
			if not par:
				throw(_("Account {0}: Parent account {1} does not exist").format(self.name, self.parent_account))
			elif par.name == self.name:
				throw(_("Account {0}: You can not assign itself as parent account").format(self.name))
			elif not par.is_group:
				throw(_("Account {0}: Parent account {1} can not be a ledger").format(self.name, self.parent_account))
			elif par.company != self.company:
				throw(_("Account {0}: Parent account {1} does not belong to company: {2}")
					.format(self.name, self.parent_account, self.company))

	def set_root_and_report_type(self):
		if self.parent_account:
			par = frappe.db.get_value("Account", self.parent_account,
				["report_type", "root_type"], as_dict=1)

			if par.report_type:
				self.report_type = par.report_type
			if par.root_type:
				self.root_type = par.root_type

		if self.is_group:
			db_value = frappe.db.get_value("Account", self.name, ["report_type", "root_type"], as_dict=1)
			if db_value:
				if self.report_type != db_value.report_type:
					frappe.db.sql("update `tabAccount` set report_type=%s where lft > %s and rgt < %s",
						(self.report_type, self.lft, self.rgt))
				if self.root_type != db_value.root_type:
					frappe.db.sql("update `tabAccount` set root_type=%s where lft > %s and rgt < %s",
						(self.root_type, self.lft, self.rgt))

		if self.root_type and not self.report_type:
			self.report_type = "Balance Sheet" \
				if self.root_type in ("Asset", "Liability", "Equity") else "Profit and Loss"

	def validate_root_details(self):
		# does not exists parent
		if frappe.db.exists("Account", self.name):
			if not frappe.db.get_value("Account", self.name, "parent_account"):
				throw(_("Root cannot be edited."), RootNotEditable)

		if not self.parent_account and not self.is_group:
			frappe.throw(_("Root Account must be a group"))

	def validate_root_company_and_sync_account_to_children(self):
		# ignore validation while creating new compnay or while syncing to child companies
		if frappe.local.flags.ignore_root_company_validation or self.flags.ignore_root_company_validation:
			return

		ancestors = get_root_company(self.company)
		if ancestors:
			if frappe.get_value("Company", self.company, "allow_account_creation_against_child_company"):
				return
			frappe.throw(_("Please add the account to root level Company - %s" % ancestors[0]))
		else:
			descendants = get_descendants_of('Company', self.company)
			if not descendants: return

			parent_acc_name_map = {}
			parent_acc_name = frappe.db.get_value('Account', self.parent_account, "account_name")
			for d in frappe.db.get_values('Account',
				{"company": ["in", descendants], "account_name": parent_acc_name},
				["company", "name"], as_dict=True):
				parent_acc_name_map[d["company"]] = d["name"]

			if not parent_acc_name_map: return

			for company in descendants:
				if not parent_acc_name_map.get(company):
					frappe.throw(_("While creating account for child Company {0}, parent account {1} not found. Please create the parent account in corresponding COA")
						.format(company, parent_acc_name))

				doc = frappe.copy_doc(self)
				doc.flags.ignore_root_company_validation = True
				doc.update({
					"company": company,
					"account_currency": None,
					"parent_account": parent_acc_name_map[company]
				})
				doc.save()
				frappe.msgprint(_("Account {0} is added in the child company {1}")
					.format(doc.name, company))

	def validate_group_or_ledger(self):
		if self.get("__islocal"):
			return

		existing_is_group = frappe.db.get_value("Account", self.name, "is_group")
		if cint(self.is_group) != cint(existing_is_group):
			if self.check_gle_exists():
				throw(_("Account with existing transaction cannot be converted to ledger"))
			elif self.is_group:
				if self.account_type and not self.flags.exclude_account_type_check:
					throw(_("Cannot covert to Group because Account Type is selected."))
			elif self.check_if_child_exists():
				throw(_("Account with child nodes cannot be set as ledger"))

	def validate_frozen_accounts_modifier(self):
		old_value = frappe.db.get_value("Account", self.name, "freeze_account")
		if old_value and old_value != self.freeze_account:
			frozen_accounts_modifier = frappe.db.get_value('Accounts Settings', None, 'frozen_accounts_modifier')
			if not frozen_accounts_modifier or \
				frozen_accounts_modifier not in frappe.get_roles():
					throw(_("You are not authorized to set Frozen value"))

	def validate_balance_must_be_debit_or_credit(self):
		from erpnext.accounts.utils import get_balance_on
		if not self.get("__islocal") and self.balance_must_be:
			account_balance = get_balance_on(self.name)

			if account_balance > 0 and self.balance_must_be == "Credit":
				frappe.throw(_("Account balance already in Debit, you are not allowed to set 'Balance Must Be' as 'Credit'"))
			elif account_balance < 0 and self.balance_must_be == "Debit":
				frappe.throw(_("Account balance already in Credit, you are not allowed to set 'Balance Must Be' as 'Debit'"))

	def validate_account_currency(self):
		if not self.account_currency:
			self.account_currency = frappe.get_cached_value('Company',  self.company,  "default_currency")

		elif self.account_currency != frappe.db.get_value("Account", self.name, "account_currency"):
			if frappe.db.get_value("GL Entry", {"account": self.name}):
				frappe.throw(_("Currency can not be changed after making entries using some other currency"))

	def convert_group_to_ledger(self):
		if self.check_if_child_exists():
			throw(_("Account with child nodes cannot be converted to ledger"))
		elif self.check_gle_exists():
			throw(_("Account with existing transaction cannot be converted to ledger"))
		else:
			self.is_group = 0
			self.save()
			return 1

	def convert_ledger_to_group(self):
		if self.check_gle_exists():
			throw(_("Account with existing transaction can not be converted to group."))
		elif self.account_type and not self.flags.exclude_account_type_check:
			throw(_("Cannot covert to Group because Account Type is selected."))
		else:
			self.is_group = 1
			self.save()
			return 1

	# Check if any previous balance exists
	def check_gle_exists(self):
		return frappe.db.get_value("GL Entry", {"account": self.name})

	def check_if_child_exists(self):
		return frappe.db.sql("""select name from `tabAccount` where parent_account = %s
			and docstatus != 2""", self.name)

	def validate_mandatory(self):
		if not self.root_type:
			throw(_("Root Type is mandatory"))

		if not self.report_type:
			throw(_("Report Type is mandatory"))

	def on_trash(self):
		# checks gl entries and if child exists
		if self.check_gle_exists():
			throw(_("Account with existing transaction can not be deleted"))

		super(Account, self).on_trash(True)

def get_parent_account(doctype, txt, searchfield, start, page_len, filters):
	return frappe.db.sql("""select name from tabAccount
		where is_group = 1 and docstatus != 2 and company = %s
		and %s like %s order by name limit %s, %s""" %
		("%s", searchfield, "%s", "%s", "%s"),
		(filters["company"], "%%%s%%" % txt, start, page_len), as_list=1)

def get_account_currency(account):
	"""Helper function to get account currency"""
	if not account:
		return
	def generator():
		account_currency, company = frappe.get_cached_value("Account", account, ["account_currency", "company"])
		if not account_currency:
			account_currency = frappe.get_cached_value('Company',  company,  "default_currency")

		return account_currency

	return frappe.local_cache("account_currency", account, generator)

def on_doctype_update():
	frappe.db.add_index("Account", ["lft", "rgt"])

def get_account_autoname(account_number, account_name, company):
	# first validate if company exists
	company = frappe.get_cached_value('Company',  company,  ["abbr", "name"], as_dict=True)
	if not company:
		frappe.throw(_('Company {0} does not exist').format(company))

	parts = [account_name.strip(), company.abbr]
	if cstr(account_number).strip():
		parts.insert(0, cstr(account_number).strip())
	return ' - '.join(parts)

def validate_account_number(name, account_number, company):
	if account_number:
		account_with_same_number = frappe.db.get_value("Account",
			{"account_number": account_number, "company": company, "name": ["!=", name]})
		if account_with_same_number:
			frappe.throw(_("Account Number {0} already used in account {1}")
				.format(account_number, account_with_same_number))

@frappe.whitelist()
def update_account_number(name, account_name, account_number=None):

	account = frappe.db.get_value("Account", name, "company", as_dict=True)
	if not account: return
	validate_account_number(name, account_number, account.company)
	if account_number:
		frappe.db.set_value("Account", name, "account_number", account_number.strip())
	else:
		frappe.db.set_value("Account", name, "account_number", "")
	frappe.db.set_value("Account", name, "account_name", account_name.strip())

	new_name = get_account_autoname(account_number, account_name, account.company)
	if name != new_name:
		frappe.rename_doc("Account", name, new_name, force=1)
		return new_name

@frappe.whitelist()
def merge_account(old, new, is_group, root_type, company):
	# Validate properties before merging
	if not frappe.db.exists("Account", new):
		throw(_("Account {0} does not exist").format(new))

	val = list(frappe.db.get_value("Account", new,
		["is_group", "root_type", "company"]))

	if val != [cint(is_group), root_type, company]:
		throw(_("""Merging is only possible if following properties are same in both records. Is Group, Root Type, Company"""))

	if is_group and frappe.db.get_value("Account", new, "parent_account") == old:
		frappe.db.set_value("Account", new, "parent_account",
			frappe.db.get_value("Account", old, "parent_account"))

	frappe.rename_doc("Account", old, new, merge=1, force=1)

	return new

@frappe.whitelist()
def get_root_company(company):
	# return the topmost company in the hierarchy
	ancestors = get_ancestors_of('Company', company, "lft asc")
	return [ancestors[0]] if ancestors else []

@frappe.whitelist()
def pendingCount(docnm):
	a= frappe.db.sql("select count(cptstatus) from `tabComplaints` where  cptstatus like '" + docnm + "';")
             
	return a

@frappe.whitelist()
def pendingCount1(count,dname):
	  frappe.db.sql("update `tabComplaintsDashboard` set cdpending= '" + count + "' AND name like '" + dname + "';")  


@frappe.whitelist()
def getFavourite(usr):
	a= frappe.db.sql("select uaarticalid,name  from `tabUserActivity` where uauserid like '"+usr+"' and uaacttype like 'Favourites' and (uaarticalid like 'HWT%' or uaarticalid like 'NW%');")
              
	return a

@frappe.whitelist()
def getRemCount(cptno):
	a= frappe.db.sql("select count(*) from `tabReminder` where ecomplaintsno like '"+cptno+"';")
              
	return a


@frappe.whitelist()
def News_cunt(docnm):
	a= frappe.db.sql("select likecount,dislikecount from (select count(uaacttype) as likecount,uaarticalid from `tabUserActivity` where  uaarticalid like '"+docnm+"' and uaacttype like 'Like') as z1,(select count(uaacttype) as dislikecount from `tabUserActivity` where  uaarticalid like '"+docnm+"' and uaacttype like 'Dislike') as z2;")
              
	return a
@frappe.whitelist()
def Newslike_cunt1(docnm,count):
	  frappe.db.sql("update `tabNews` set nwlike= '" + count + "' where name like '" + docnm + "';")  

@frappe.whitelist()
def Newsdislike_cunt1(docnm,count):
	  frappe.db.sql("update `tabNews` set nwdislike= '" + count + "' where name like '" + docnm + "';")



@frappe.whitelist()
def Eventslike_cunt(docnm,acttype):
	a= frappe.db.sql("select count(uaacttype) + 1 from `tabUserActivity` where  uaarticalid like '" + docnm + "' and uaacttype like '" + acttype + "';")
             
	return a

@frappe.whitelist()
def Eventslike_cunt1(docnm,evncount):
	  frappe.db.sql("update `tabEvents` set evnlike= '" + evncount + "' where name like '" + docnm + "';")  

@frappe.whitelist()
def Eventsdislike_cunt1(docnm,evncount):
	  frappe.db.sql("update `tabEvents` set evndislike= '" + evncount + "' where name like '" + docnm + "';")




@frappe.whitelist()
def HOW_TOslike_cunt():
     frappe.db.sql("update `tabHOW_TOs` h set htlike = (select count(*) from `tabUserActivity` where h.name=uaarticalid and uaacttype like 'Like'),htdislike = (select count(*) from `tabUserActivity` where h.name=uaarticalid and uaacttype like 'Dislike');")


@frappe.whitelist()
def HOW_TOslike_cunt1(docnm,hwtcount):
	  frappe.db.sql("update `tabHOW_TOs` set htlike= '" + hwtcount + "' where name like '" + docnm + "';")  

@frappe.whitelist()
def HOW_TOsdislike_cunt1(docnm,hwtcount):
	  frappe.db.sql("update `tabHOW_TOs` set htdislike= '" + hwtcount + "' where name like '" + docnm + "';")
     
@frappe.whitelist()
def SurveyCount():
	  frappe.db.sql("update `tabSurveyActivity` s set sagreat=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'Great'),sagood=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'Good'),sabad=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'Bad'),saokay=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'Okay'),saterrible=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'Terrible'),sanone=(select count(*) from `tabSurveyAnsTable` where parent= s.name and surananswer like 'None');")


@frappe.whitelist()
def pump_names():
    a= frappe.db.sql(" select distinct pump_name from `tabDaily_Fuel_Rate`;")
    return a

@frappe.whitelist()
def vehicle_type():
    item_details = {}
    data=[]
    a= frappe.db.sql("select distinct vehicle_type from `tabAsset` where item_code like 'Vehicle';", as_dict=1)
    for d in a:
      item_details[d.vehicle_type] = d
      data.append(d)
    return data

@frappe.whitelist()
def reason():
    item_details = {}
    data=[]
    a= frappe.db.sql("select 'Vehicle Breakdown' as reason_for_breakdown union select 'Maintenance' as reason_for_breakdown union select distinct '' as reason_for_breakdown from `tabAsset`;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def vehicle_deployed_all(id,vehicle_type,reason):
    item_details = {}
    data=[]
    if vehicle_type=='NA' and reason=='NA' :
      a= frappe.db.sql("select am.da_vehicleno,am.da_drivername,	am.fawardno,	am.r_name,	wm.supervisor_id,	wm.area_manager_id,	'Deployed' as status,i.vehicle_type,'' as reason_for_breakdown,type as poi	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabAsset` i,`tabDriverActivity` rm  where rm.name=am.da_code and rm.r_status like 'In-Progress' and i.registration_number = am.da_vehicleno and wm.ward_no=am.fawardno 	and date_format(rm.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"') group by am.da_vehicleno	UNION	select registration_number,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"'));", as_dict=1)
    if vehicle_type!='NA' and reason=='NA' :
      a= frappe.db.sql("select am.da_vehicleno,am.da_drivername,	am.fawardno,	am.r_name,	wm.supervisor_id,	wm.area_manager_id,	'Deployed' as status,i.vehicle_type,'' as reason_for_breakdown,type as poi	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabAsset` i,`tabDriverActivity` rm  where rm.name=am.da_code and rm.r_status like 'In-Progress' and i.registration_number = am.da_vehicleno and wm.ward_no=am.fawardno 	and date_format(rm.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"') and i.vehicle_type like '"+vehicle_type+"' group by am.da_vehicleno	UNION	select registration_number,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) and vehicle_type like '"+vehicle_type+"';", as_dict=1)
    if vehicle_type=='NA' and reason!='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) and reason_for_breakdown like '"+reason+"';", as_dict=1)
    if vehicle_type!='NA' and reason!='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) and vehicle_type like '"+vehicle_type+"' and reason_for_breakdown like '"+reason+"';", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
    
@frappe.whitelist()
def vehicle_not_deployed(id,vehicle_type,reason):
    item_details = {}
    data=[]
    if vehicle_type!='NA' and reason!='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) and vehicle_type like '"+vehicle_type+"';", as_dict=1)
    if vehicle_type=='NA' and reason=='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,vehicle_type ,'' as fawardno,'' as r_name,'' as supervisor_id,'' as area_manager_id ,'Not Deployed' as status,'' as reason_for_breakdown,'' as type from `tabAsset` where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"'))", as_dict=1)
    if vehicle_type=='NA' and reason!='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) ;", as_dict=1) 
    if vehicle_type!='NA' and reason=='NA':
      a= frappe.db.sql("select registration_number as da_vehicleno,'' as da_drivername,	'' as fawardno,	'' as r_name,	'' as supervisor_id,	'' as area_manager_id,	'Not Deployed' as status,vehicle_type,'' as reason_for_breakdown,'' as poi	from `tabAsset` 	where vehicle_type like '"+vehicle_type+"' and item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"'));", as_dict=1)    
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def vehicle_deployed(id,vehicle_type):
    item_details = {}
    data=[]
    if vehicle_type!='NA':
       a= frappe.db.sql("select am.da_vehicleno,am.da_drivername,	am.fawardno,	r_name,	wm.supervisor_id,	wm.area_manager_id,	'Deployed' as status,a.item_code as poi,a.vehicle_type	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabAsset` a  	where a.registration_number=am.da_vehicleno and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"') and a.vehicle_type like '"+vehicle_type+"' group by am.da_vehicleno;", as_dict=1)
    if vehicle_type=='NA':    
      a= frappe.db.sql("select am.da_vehicleno,am.da_drivername,	am.fawardno,	r_name,	wm.supervisor_id,	wm.area_manager_id,	'Deployed' as status,type as poi,vehicle_type	from `tabActivityMaster` am,	`tabWard_Master` wm ,`tabAsset` i 	where i.registration_number=am.da_vehicleno and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"') and i.registration_number in (select distinct am.da_vehicleno	from `tabActivityMaster` am,	`tabWard_Master` wm,`tabDriverActivity` da 	where am.da_code=da.name and da.r_status not like 'Complete' and wm.ward_no=am.fawardno 	and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"')) group by am.da_vehicleno;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data


@frappe.whitelist()
def vehicle_deployed_count(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select z2.not_deployed,count(z3.da_vehicleno) as deployed,z2.not_deployed+count(z3.da_vehicleno) as all_deployed from (select count(registration_number) as not_deployed from `tabAsset` where item_code like 'Vehicle' and registration_number not in (select distinct am.da_vehicleno from `tabActivityMaster` am,`tabWard_Master` wm where wm.ward_no=am.fawardno and date_format(am.creation,'%Y%m%d') like date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"'))) as z2,	(select am.da_vehicleno from `tabActivityMaster` am,`tabWard_Master` wm,`tabDriverActivity` rm  where rm.da_routeid=am.da_routeid and rm.r_status like 'In-Progress' and  wm.ward_no=am.fawardno and date_format(rm.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and date_format(am.creation,'%Y%m%d') like  date_format(now(),'%Y%m%d') and (wm.supervisor_id like '"+id+"' or area_manager_id like '"+id+"') group by am.da_vehicleno) as z3;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
    
@frappe.whitelist()
def bin_status(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select name as item_name,route_info as da_routeid,asset_name ,'Pending' as fastatus,ward_no,capacity,item_code,geo_location,image,address,longitude,latitude, null as modified from `tabAsset`  where ward_no='"+id+"' and route_info is not null and name not in (select distinct route_location_name from `tabActivityMaster` am, `tabDriverActivity` da where am.da_code=da.name and  am.clean_status in ('Scheduled','Clean') and da.r_status like 'In-Progress' and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%')) and item_code not like 'Vehicle' and geo_location is not null union select i.name,am.da_routeid, i.asset_name ,'Scheduled' as fastatus,i.ward_no,i.capacity,i.item_code,i.geo_location,i.image,i.address,i.longitude,i.latitude,am.modified from `tabAsset` i,`tabActivityMaster` am, `tabDriverActivity` da  where i.ward_no='"+id+"' and i.route_info is not null and i.name = route_location_name and am.da_code=da.name and  am.clean_status like 'Scheduled' and da.r_status like 'In-Progress' and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%') and item_code not like 'Vehicle' and geo_location is not null union select i.name, am.da_routeid,i.asset_name ,'Clean' as fastatus,i.ward_no,i.capacity,i.item_code,i.geo_location,i.image,i.address,i.longitude,i.latitude,am.modified from `tabAsset` i,`tabActivityMaster` am, `tabDriverActivity` da  where i.ward_no='"+id+"' and i.route_info is not null and i.name = route_location_name and am.da_code=da.name and  am.clean_status like 'Clean' and da.r_status in ('In-Progress','Complete') and str_to_date(date_format(am.creation,'%Y%m%d%H%i'),'%Y%m%d%H%i') > str_to_date(date_format(date_sub(now(),interval 6 hour),'%Y%m%d%H%i'),'%Y%m%d%H%i') and item_code not like 'Vehicle' and geo_location is not null;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
    
@frappe.whitelist()
def bin_cleared(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select am.type as asset_type,route_location_name,clean_status,wm.ward_no,route_assetcap from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = item_name and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') group by route_location_name;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
    
@frappe.whitelist()
def ward_no(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select distinct ward_no from `tabWard_Master` where area_manager_id like '"+id+"' or supervisor_id like '"+id+"';", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
    
@frappe.whitelist()
def route_info(id):
    item_details = {}
    data=[]
    a= frappe.db.sql(" select distinct parent as r_name  from `tabRoute Info` where fawardno like '"+id+"';", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def bin_cleared_filter(id,ward_no,route_no,status,for_date):
    item_details = {}
    data=[]
    if status=='NA' and for_date=='NA':
        a= frappe.db.sql("select item_name as asset_type,asset_name,'Pending' as fastatus,ward_no,capacity ,image,longitude,latitude,route_info as da_routeid,geo_location,address,item_code  from `tabAsset` where item_code not like 'Vehicle' and name not in (select route_location_name from `tabActivityMaster` where date_format(creation,'%Y,%m,%d')=date_format(now(),'%Y,%m,%d')) and ward_no like '"+ward_no+"' and route_info like '"+route_no+"' union select am.type as asset_type,asset_name as item_name,clean_status as fastatus,wm.ward_no as ward_no,route_assetcap as capacity,i.image,longitude,latitude,am.da_routeid,i.geo_location,i.address,i.item_code from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = i.name and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') and wm.ward_no like '"+ward_no+"' and am.da_routeid like '"+route_no+"' group by route_location_name;", as_dict=1)
    if status=='NA' and for_date!='NA':
          a= frappe.db.sql("select am.type as asset_type,am.da_routeid,i.asset_name as asset_name,clean_status as fastatus,wm.ward_no as ward_no,route_assetcap as capacity,am.modified as for_date,i.image,longitude,latitude,i.route_info,i.geo_location,i.address,i.item_code from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = i.name and am.creation like concat('"+for_date+"','%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') and wm.ward_no like '"+ward_no+"' and am.da_routeid like '"+route_no+"' group by route_location_name;", as_dict=1)
    if status!='NA' and for_date=='NA':
      if status=='Clean':
        a= frappe.db.sql("select am.type as asset_type,i.asset_name,clean_status as fastatus,wm.ward_no as ward_no,route_assetcap as capacity,i.image,longitude,latitude,am.da_routeid,i.geo_location,i.address,i.item_code from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = i.name and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') and wm.ward_no like '"+ward_no+"' and am.da_routeid like '"+route_no+"' and clean_status like '"+status+"' group by route_location_name order by am.modified desc;", as_dict=1)
      if status=='Pending':
        a= frappe.db.sql("select asset_name,'Pending' as fastatus,ward_no as ward_no,capacity,item_code,geo_location,route_info,image,address,longitude,latitude,route_info as da_routeid,geo_location,address,item_code from `tabAsset` where item_code not like 'Vehicle' and name not in (select route_location_name from `tabActivityMaster` where date_format(creation,'%Y,%m,%d')=date_format(now(),'%Y,%m,%d') and fawardno like '"+ward_no+"' and da_routeid  is not null) ;", as_dict=1)
      if status=='Scheduled':
        a= frappe.db.sql("select am.type as asset_type,i.asset_name,'Scheduled' as fastatus,wm.ward_no as ward_no,route_assetcap as capacity,i.image,longitude,latitude,am.da_routeid,i.geo_location,i.address,i.item_code from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = i.name and am.creation like concat(date_format(date_sub(now(),interval 0 day),'%Y-%m-%d'),'%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') and wm.ward_no like '"+ward_no+"' and am.da_routeid  like '"+route_no+"' and clean_status like 'Scheduled' group by route_location_name order by am.modified desc;", as_dict=1)
    if status!='NA' and for_date!='NA':
          a= frappe.db.sql("select am.type as asset_type,asset_name,clean_status as fastatus,wm.ward_no,route_assetcap as capacity,am.modified as for_date,i.image,longitude,latitude,am.da_routeid,i.geo_location,i.address,i.item_code from `tabActivityMaster` am,`tabAsset` i,`tabWard_Master` wm  where route_location_name = i.name and am.creation like concat('"+for_date+"','%') and wm.ward_no=am.fawardno and (wm.supervisor_id like '"+id+"' or wm.area_manager_id like '"+id+"') and wm.ward_no like '"+ward_no+"' and am.da_routeid like '"+route_no+"' and clean_status like '"+status+"' group by route_location_name order by am.modified desc;", as_dict=1)    
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def route_coverage_summary(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select distinct da_routeid,da_vehicleno,da_drivername,r_status,da.creation,da.modified,vehicle_type,fawardno,da.name from `tabDriverActivity` da,`tabRoute Info` ri  where ri.parent=da.da_routeid and date_format(da.creation,'%Y%m%d') like concat(date_format(now(),'%Y%m%d'),'%') and fawardno in (select distinct ward_no from `tabWard_Master` where supervisor_id like '"+id+"' or area_manager_id like '"+id+"');", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def driver_not_assigned():
    item_details = {}
    data=[]
    a= frappe.db.sql("select employee_name,designation,name,regmobile from `tabEmployee` where employee_name not in (select distinct da_drivername from `tabDriverActivity` where r_status not like 'Complete' and date_format(creation,'%Y%m%d')=date_format(now(),'%Y%m%d')) and designation like 'Driver' and status like 'Active';", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def supervisordetails(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select name as supervisor_id, employee_name as supervisor_name from `tabEmployee` where name in (select supervisor_id from `tabWard_Master` where parent is not null and ward_no in (select ward_no from `tabAsset` where registration_number='"+id+"'));", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def notification(id):
    item_details = {}
    data=[]
    a= frappe.db.sql("select 'Event' as nftdoctypename,evntitle as nftdesc from `tabEvents` where date_format(creation,'%Y%m%d')=date_format(now(),'%Y%m%d') UNION select 'News' as nftdoctypename,nwtitle as nftdesc from `tabNews` where date_format(creation,'%Y%m%d')=date_format(now(),'%Y%m%d') UNION select concat('Status of your Complaint',' ',name ) as nftdoctypename  ,cptstatus as nftdesc from `tabComplaints` where date_format(modified,'%Y%m%d')=date_format(now(),'%Y%m%d') and cptuserid like '"+id+"';", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def update_route_master():
    a= frappe.db.sql("update `tabRoute Info` r,`tabAsset` a set route_lat=latitude,route_long=longitude,route_assetcap=capacity,r.barcode=a.barcode where r.route_location_name=a.name and a.geo_location is not null;")
    return a

@frappe.whitelist()
def update_route_in_asset_master():
    a= frappe.db.sql("update `tabRoute Info` r,`tabAsset` a set a.route_info=r.parent,fawardno=ward_no where r.route_location_name=a.name;")
    return a

@frappe.whitelist()
def get_deployed_vehicle(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select z1.on_road ,z2.idle,z3.under_maintainance,z4.reserved,z1.on_road +z2.idle+z3.under_maintainance+z4.reserved as total\
from \
(select count(distinct (da_vehicleno)) as on_road \
from `tabActivityMaster` am \
where \
str_to_date(date_format(am.creation,'%Y-%m-%d'),'%Y-%m-%d')\
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')\
) z1,\
(select count(registration_number) as idle from `tabAsset` where item_name like 'Vehicle' and docstatus =1 and route_info is null) z2,\
(\
select count(registration_number) as under_maintainance \
from `tabAsset` \
where item_name like 'Vehicle' \
and docstatus =1 \
and registration_number in \
	(select m.registration_number from `tabAsset Maintenance` m ,		`tabAsset Maintenance Log` ml where ml.parent=m.name and 		str_to_date(date_format(ml.completion_date,'%Y-%m-%d'),'%Y-%m-%d')\
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')\
	)\
) z3,\
(select count(registration_number) as reserved \
from `tabAsset` where item_name like 'Vehicle' and route_info is not null and registration_number not in (select distinct da_vehicleno \
from `tabActivityMaster` am \
where \
str_to_date(date_format(am.creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')\
group by da_vehicleno)) z4;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def complaint_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select distinct monthname(creation) as mon,\
(select count(cptstatus) as new from `tabComplaints` where cptstatus like 'New' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and monthname(creation) = mon) as new, \
(select count(cptstatus) as pending from `tabComplaints` where cptstatus like 'Pending' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and monthname(creation) = mon ) as pending,\
(select count(cptstatus) as wip from `tabComplaints` where cptstatus like 'In-Process' and  str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and monthname(creation) = mon ) as wip,\
(select count(cptstatus) as closed from `tabComplaints` where cptstatus like 'Complete' and  str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and monthname(creation) = mon ) as closed \
 from `tabComplaints` group by mona", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def servicerequest_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select new,pending,wip,closed from \
(select count(cptstatus) as new from `tabComplaints` where cptstatus like 'New' and user_type like 'Corporate' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') ) z1, \
(select count(cptstatus) as pending from `tabComplaints` where cptstatus like 'Pending' and user_type like 'Corporate' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') ) z2,\
(select count(cptstatus) as wip from `tabComplaints` where cptstatus like 'In-Process' and user_type like 'Corporate' and  str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') )z3,\
(select count(cptstatus) as closed from `tabComplaints` where cptstatus like 'Complete' and user_type like 'Corporate' and  str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d') \
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')) z4", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def collections_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select newcollections,scheduled,ontime,delay from  (select count(*) as newcollections from `tabAsset` where route_info is null) z1,\
(select count(*) as scheduled from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Scheduled' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d'))) z2,\
(select count(*) as ontime from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Clean' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')))z4,\
(select count(*) as delay from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Pending' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and creation < date_sub(modified,interval 6 hour)))z5 ;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def assetcount_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select  bin,vehicle from (select count(name) as bin from `tabAsset` where item_code like 'BIN' group by item_code) z1,(select count(name) as vehicle from `tabAsset` where item_code like 'Vehicle' group by item_code) z2;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def assetstatus_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select inprogress,clean,pending from  (select count(*) as newcollections from `tabAsset` where route_info is null) z1,\
(select count(*) as inprogress from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Scheduled' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d'))) z2,\
(select count(*) as clean from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Clean' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d')))z4,\
(select count(*) as pending from `tabAsset` where name in (select route_location_name from `tabActivityMaster` where clean_status like 'Pending' and str_to_date(date_format(creation,'%Y-%m-%d'),'%Y-%m-%d') \
between str_to_date(date_format('"+fromdt+"','%Y-%m-%d'),'%Y-%m-%d')\
and str_to_date(date_format('"+todt+"','%Y-%m-%d'),'%Y-%m-%d') and creation < date_sub(modified,interval 6 hour)))z5 ;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def assetsclearance_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select round((cleanstatus/total)*100,2) as clean_per,round((scheduled/total)*100,2) as scheduled_per,round((newcollections/totalcollections)*100,2) as not_considered \
from (select count(name) as total from `tabActivityMaster`) z1,\
(select count(name) as cleanstatus from `tabActivityMaster` where clean_status like 'Clean')z2,\
(select count(name) as scheduled from `tabActivityMaster` where clean_status like 'Scheduled')z3,\
(select count(name) as pending from `tabActivityMaster` where clean_status like 'Pending')z4,\
(select count(*) as newcollections from `tabAsset` where route_info is null and name not in (select route_location_name from `tabActivityMaster`) and item_code not in ('Vehicle') )z5,\
(select count(*) as totalcollections from `tabAsset` where item_code not in ('Vehicle') )z6;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def assetstable_dashboard(fromdt,todt,project):
    item_details = {}
    data=[]
    a= frappe.db.sql("select route_location_name,da_routeid,route_assetloc,da_vehicleno,da_drivername,clean_status from `tabActivityMaster` where date_format(creation,'%Y-%m-%d')=date_format(now(),'%Y-%m-%d') order by creation;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data

@frappe.whitelist()
def type_of_vehicle_dashboard(project):
    item_details = {}
    data=[]
    a= frappe.db.sql("Select vehicle_type,count(name) as vehicle_count from `tabAsset` where item_code like 'Vehicle' and company like '"+project+"' group by vehicle_type;", as_dict=1)
    for d in a:
      item_details[d.da_vehicleno] = d
      data.append(d)
    return data
