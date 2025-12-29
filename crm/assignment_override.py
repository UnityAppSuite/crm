import json

import frappe
import frappe.share
import frappe.utils
from frappe import _
from frappe.desk.form.document_follow import follow_document
from frappe.utils.data import strip_html

@frappe.whitelist()
def add_assignment_without_email(args=None, *, ignore_permissions=False):
	"""
	Custom assignment function that creates ToDo assignments WITHOUT sending email notifications.
	This overrides frappe.desk.form.assign_to.add() to disable emails while preserving all other functionality.
	"""
	if not args:
		args = frappe.local.form_dict

	users_with_duplicate_todo = []
	shared_with_users = []

	for assign_to in frappe.parse_json(args.get("assign_to")):
		filters = {
			"reference_type": args["doctype"],
			"reference_name": args["name"],
			"status": "Open",
			"allocated_to": assign_to,
		}
		if not ignore_permissions:
			frappe.get_doc(args["doctype"], args["name"]).check_permission()

		if frappe.get_all("ToDo", filters=filters):
			users_with_duplicate_todo.append(assign_to)
		else:
			from frappe.utils import nowdate

			description = args.get("description") or ""
			has_content = strip_html(description) or "<img" in description
			if not has_content:
				args["description"] = _("Assignment for {0} {1}").format(args["doctype"], args["name"])

			# Create ToDo document
			d = frappe.get_doc(
				{
					"doctype": "ToDo",
					"allocated_to": assign_to,
					"reference_type": args["doctype"],
					"reference_name": str(args["name"]),
					"description": args.get("description"),
					"priority": args.get("priority", "Medium"),
					"status": "Open",
					"date": args.get("date", nowdate()),
					"assigned_by": args.get("assigned_by", frappe.session.user),
					"assignment_rule": args.get("assignment_rule"),
				}
			).insert(ignore_permissions=True)

			# set assigned_to if field exists
			if frappe.get_meta(args["doctype"]).get_field("assigned_to"):
				frappe.db.set_value(args["doctype"], args["name"], "assigned_to", assign_to)

			doc = frappe.get_doc(args["doctype"], args["name"])

			# if assignee does not have permissions, share or inform
			if not frappe.has_permission(doc=doc, user=assign_to):
				if frappe.get_system_settings("disable_document_sharing"):
					msg = _("User {0} is not permitted to access this document.").format(
						frappe.bold(assign_to)
					)
					msg += "<br>" + _(
						"As document sharing is disabled, please give them the required permissions before assigning."
					)
					frappe.throw(msg, title=_("Missing Permission"))
				else:
					frappe.share.add(doc.doctype, doc.name, assign_to)
					shared_with_users.append(assign_to)

			# make this document followed by assigned user
			if frappe.get_cached_value("User", assign_to, "follow_assigned_documents"):
				follow_document(args["doctype"], args["name"], assign_to)

			# LOG ASSIGNMENT (instead of sending email notification)
			frappe.logger().info(f"Assignment created WITHOUT email notification: {args['doctype']} {args['name']} assigned to {assign_to} by {frappe.session.user}")
			
			# SKIP: notify_assignment() call - this is the key change that prevents emails

	if shared_with_users:
		user_list = format_message_for_assign_to(shared_with_users)
		frappe.msgprint(
			_("Shared with the following Users with Read access:{0}").format(user_list, alert=True)
		)

	if users_with_duplicate_todo:
		user_list = format_message_for_assign_to(users_with_duplicate_todo)
		frappe.msgprint(_("Already in the following Users ToDo list:{0}").format(user_list, alert=True))

	# Return assignment data (same as original function)
	return frappe.get_all(
		"ToDo",
		fields=["allocated_to as owner", "name"],
		filters={
			"reference_type": args.get("doctype"),
			"reference_name": args.get("name"),
			"status": ("not in", ("Cancelled", "Closed")),
		},
		limit=5,
	)


def enqueue_create_notification_without_assignment_emails(users, doc):
	"""
	Override function that filters out Task Assignment type notifications to prevent emails.
	CRM and other assignment notifications will still send emails normally.
	Only blocks assignments for Task doctype.
	"""
	import frappe

	doc = frappe._dict(doc)
	
	# Debug logging to see if override is working
	frappe.logger().info(f"OVERRIDE CALLED - Type: {doc.get('type')}, DocType: {doc.get('document_type')}, Subject: {doc.get('subject', 'No subject')}")
	
	# Block Assignment notifications based on the following rules:
	# 1. Block ALL Task assignments and removals
	# 2. Block ALL removal notifications (for any doctype)
	# 3. Allow assignment notifications for CRM (Lead, Deal, etc.)
	if doc.get("type") == "Assignment":
		subject = doc.get("subject", "").lower()
		is_removal = "removed" in subject or "has been removed" in subject
		is_task = doc.get("document_type") == "Task"
		
		if is_task:
			action_type = "assignment" if "assigned" in subject else "removal"
			frappe.logger().info(f"Task {action_type} notification blocked: {doc.get('subject', 'No subject')} for users: {users}")
			return  # Block all Task notifications
		elif is_removal:
			frappe.logger().info(f"Assignment removal notification blocked: {doc.get('subject', 'No subject')} for users: {users}")
			return  # Block all removal notifications (CRM, etc.)
	
	# Allow all other assignment notifications (CRM Lead, CRM Deal, etc.) to proceed normally
	
	# For all other notification types, recreate the original logic without circular import
	# During installation of new site, enqueue_create_notification tries to connect to Redis.
	# This breaks new site creation if Redis server is not running.
	# We do not need any notifications in fresh installation
	if frappe.flags.in_install:
		return

	if isinstance(users, str):
		users = [user.strip() for user in users.split(",") if user.strip()]
	users = list(set(users))

	frappe.enqueue(
		"frappe.desk.doctype.notification_log.notification_log.make_notification_logs",
		doc=doc,
		users=users,
		now=frappe.flags.in_test,
	)


def prevent_assignment_notifications(doc, method):
    """
    Prevent email notifications for specific assignment cases:
    1. All Task assignments and removals
    2. All assignment removals (any doctype)
    3. Allow only CRM assignments (not removals)
    """
    if method in ["on_update", "after_save", "validate"]:
        # Check if this is an assignment removal (status changed to Cancelled)
        if doc.status == "Cancelled":
            # Block ALL removal notifications by setting a flag
            frappe.local.flags.setdefault("blocked_notifications", []).append({
                "type": "removal",
                "doctype": doc.reference_type,
                "docname": doc.reference_name,
                "user": doc.allocated_to
            })
            frappe.logger().info(f"Assignment removal blocked for {doc.reference_type} {doc.reference_name} -> {doc.allocated_to}")
            
        elif doc.status == "Open" and doc.reference_type == "Task":
            # Block Task assignment notifications
            frappe.local.flags.setdefault("blocked_notifications", []).append({
                "type": "assignment", 
                "doctype": doc.reference_type,
                "docname": doc.reference_name,
                "user": doc.allocated_to
            })
            frappe.logger().info(f"Task assignment blocked for {doc.reference_type} {doc.reference_name} -> {doc.allocated_to}")


@frappe.whitelist()
def custom_assignment_add(args=None, *, ignore_permissions=False):
    """
    Custom assignment function for Tasks that prevents email notifications
    """
    # For non-Task assignments, use the original function
    if args and args.get("doctype") != "Task":
        # Import and call original function
        from frappe.desk.form.assign_to import add as original_add
        return original_add(args, ignore_permissions=ignore_permissions)
    
    # For Task assignments, use our custom function without emails
    return add_assignment_without_email(args, ignore_permissions=ignore_permissions)


def custom_notify_assignment(assigned_by, allocated_to, doc_type, doc_name, action="CLOSE", description=None):
    """
    Custom notify_assignment that blocks notifications based on our rules
    """
    # Block all Task notifications
    if doc_type == "Task":
        action_type = "assignment" if action == "ASSIGN" else "removal"
        frappe.logger().info(f"Task {action_type} notification blocked: {doc_type} {doc_name} -> {allocated_to}")
        return
    
    # Block all removal notifications (action="CLOSE" means removal)
    if action == "CLOSE":
        frappe.logger().info(f"Assignment removal notification blocked: {doc_type} {doc_name} -> {allocated_to}")
        return
    
    # Allow CRM assignment notifications (action="ASSIGN" for CRM)
    if action == "ASSIGN":
        frappe.logger().info(f"CRM assignment notification allowed: {doc_type} {doc_name} -> {allocated_to}")
        # Call the original notify_assignment function
        from frappe.desk.form.assign_to import notify_assignment as original_notify
        return original_notify(assigned_by, allocated_to, doc_type, doc_name, action, description)


def format_message_for_assign_to(users):
	"""Helper function to format user list messages"""
	return "<br><br>" + "<br>".join(users)