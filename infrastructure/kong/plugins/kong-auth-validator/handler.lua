-- Kong Auth Validator Plugin
-- Validates tokens against auth service and adds user context to headers

local http = require "resty.http"
local cjson = require "cjson"

local KongAuthValidator = {
  PRIORITY = 1000,
  VERSION = "1.0.0",
}

function KongAuthValidator:access(config)
  local httpc = http.new()
  
  -- Extract token from Authorization header
  local auth_header = kong.request.get_header("authorization")
  if not auth_header then
    return kong.response.exit(401, {
      message = "Authorization header required",
      error_code = "MISSING_AUTH_HEADER"
    })
  end
  
  -- Extract Bearer token
  local token = auth_header:match("Bearer%s+(.+)")
  if not token then
    return kong.response.exit(401, {
      message = "Invalid authorization header format",
      error_code = "INVALID_AUTH_FORMAT"
    })
  end
  
  -- Prepare validation request
  local validation_body = {
    token = token,
    required_permissions = config.required_permissions or {},
    resource = config.resource
  }
  
  -- Call auth service validation endpoint
  local res, err = httpc:request_uri(config.auth_service_url, {
    method = "POST",
    body = cjson.encode(validation_body),
    headers = {
      ["Content-Type"] = "application/json",
      ["User-Agent"] = "Kong-Auth-Validator/1.0.0"
    },
    timeout = 5000,
    keepalive_timeout = 60000,
    keepalive_pool = 10
  })
  
  if not res then
    kong.log.err("Failed to validate token: ", err)
    return kong.response.exit(503, {
      message = "Authentication service unavailable",
      error_code = "AUTH_SERVICE_ERROR"
    })
  end
  
  -- Parse response
  local validation_result
  if res.body then
    local ok, decoded = pcall(cjson.decode, res.body)
    if ok then
      validation_result = decoded
    else
      kong.log.err("Failed to decode auth service response")
      return kong.response.exit(503, {
        message = "Invalid auth service response",
        error_code = "INVALID_AUTH_RESPONSE"
      })
    end
  end
  
  -- Check if token is valid
  if not validation_result or not validation_result.valid then
    local error_message = "Token validation failed"
    local error_code = "INVALID_TOKEN"
    
    if validation_result and validation_result.error then
      error_message = validation_result.error
      error_code = validation_result.error_code or "VALIDATION_ERROR"
    end
    
    return kong.response.exit(401, {
      message = error_message,
      error_code = error_code
    })
  end
  
  -- Add user context headers for downstream services
  if validation_result.user_id then
    kong.service.request.set_header("X-User-ID", validation_result.user_id)
  end
  
  if validation_result.username then
    kong.service.request.set_header("X-Username", validation_result.username)
  end
  
  if validation_result.email then
    kong.service.request.set_header("X-User-Email", validation_result.email)
  end
  
  if validation_result.roles then
    kong.service.request.set_header("X-User-Roles", table.concat(validation_result.roles, ","))
  end
  
  if validation_result.permissions then
    kong.service.request.set_header("X-User-Permissions", table.concat(validation_result.permissions, ","))
  end
  
  if validation_result.scopes then
    kong.service.request.set_header("X-User-Scopes", table.concat(validation_result.scopes, ","))
  end
  
  -- Set user context in Kong context for other plugins
  kong.ctx.shared.user_id = validation_result.user_id
  kong.ctx.shared.username = validation_result.username
  kong.ctx.shared.user_email = validation_result.email
  kong.ctx.shared.user_roles = validation_result.roles
  kong.ctx.shared.user_permissions = validation_result.permissions
  kong.ctx.shared.user_scopes = validation_result.scopes
  
  kong.log.info("Token validated successfully for user: ", validation_result.username)
end

return KongAuthValidator
