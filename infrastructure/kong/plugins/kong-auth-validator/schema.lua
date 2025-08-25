-- Kong Auth Validator Plugin Schema
local typedefs = require "kong.db.schema.typedefs"

return {
  name = "kong-auth-validator",
  fields = {
    {
      config = {
        type = "record",
        fields = {
          {
            auth_service_url = {
              type = "string",
              required = true,
              description = "URL of the authentication service validation endpoint"
            }
          },
          {
            required_permissions = {
              type = "array",
              elements = { type = "string" },
              default = {},
              description = "List of required permissions for this route"
            }
          },
          {
            resource = {
              type = "string",
              description = "Resource identifier for permission checking"
            }
          },
          {
            cache_ttl = {
              type = "integer",
              default = 300,
              description = "Cache TTL for validation results in seconds"
            }
          },
          {
            timeout = {
              type = "integer",
              default = 5000,
              description = "Timeout for auth service requests in milliseconds"
            }
          },
          {
            keepalive_timeout = {
              type = "integer", 
              default = 60000,
              description = "Keepalive timeout in milliseconds"
            }
          },
          {
            keepalive_pool = {
              type = "integer",
              default = 10,
              description = "Keepalive pool size"
            }
          }
        }
      }
    }
  }
}
