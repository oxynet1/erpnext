frappe.listview_settings['Interview Details'] = {
     onload: function (listview) {
	      // alert("hi");      
	      
	        var user_1=user;
 		//alert(user_1);
                 //alert("Hi"); 
                 frappe.call({                        
				method: "erpnext.hr.doctype.employee.employee.get_role_1", 
				args: { 					    
					    "role": user_1,
				      },
				 callback: function(r) {
					   // return r.message.role;
                                          console.log(r.message[0].role);
                                     if(r.message[0].role.indexOf("Administrator") == -1 || r.message[0].role.indexOf("HR Manager") == -1)
                                       {
					 //alert("HiIIIIIIIIIIIIIIIi");
                                          //frappe.route_options = {"approver_1_email": ["=", user]};
					  frappe.route_options = {"interviewer_email": user};
					  frappe.set_route("List","Interview Details")
                                       }
                                    
				    
				     
				 }
		 });    

                  
					
                                                    
                

        
     }
};
