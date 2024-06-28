import asyncio
from typing import Callable
from urllib.parse import urlencode

from fastapi import (
    Request,
    HTTPException,
    WebSocket,
    WebSocketException,
)

from cat.auth.utils import (
    AuthPermission,
    AuthResource,
    AuthUserInfo,
)
from cat.looking_glass.stray_cat import StrayCat
from cat.log import log


async def http_extract_credential(request: Request) -> str | None:
    """
    Get JWT token or api key passed with the request
    """

    # Proper Authorization header
    authorization_header = request.headers.get("Authorization")
    if authorization_header and ("Bearer " in authorization_header):
        return authorization_header.replace("Bearer ", "")

    # Legacy header to pass CCAT_API_KEY
    access_token_header = request.headers.get("access_token")
    if access_token_header:
        log.warning(
            "Deprecation Warning: `access_token` header will not be supported in v2."
            "Pass your token/key using the `Authorization: Bearer <token>` format."
        )
        return access_token_header

    # no token found
    return None


async def ws_extract_credential(websocket: WebSocket) -> str | None:
    """
    Extract token from WebSocket query string
    """
    # TODOAUTH: is there a more secure way to pass the token over websocket?
    #   Headers do not work from the browser
    return websocket.query_params.get("token", None)


def ws_auth(resource: AuthResource, permission: AuthPermission) -> Callable:
    """ws_auth factory.

    Parameters
    ----------
    resource : AuthResource
        requested resource.
    permission : AuthPermission
        requested permission.

    Returns
    -------
    StrayCat
        Authorized user's StrayCat instance.

    Raises
    ------
    WebsocketDisconnect
        Websocket disconnection

    """

    async def ws_auth(
        websocket: WebSocket,
        user_id: str = "user",  # legacy user_id passed in websocket url path
    ) -> None | StrayCat:
        """Authenticate websocket connection.

        Parameters
        ----------
        websocket : WebSocket
            Websocket connection.

        Returns
        -------
        None
            Does not raise an exception if the message is allowed.

        Raises
        ------
        WebsocketDisconnect
            Websocket disconnection

        """

        async def get_user_stray(user: AuthUserInfo):
            strays = websocket.app.state.strays

            if user.id in strays.keys():
                stray = strays[user.id]
                # Close previus ws connection
                if stray._StrayCat__ws:
                    await stray._StrayCat__ws.close()
                # Set new ws connection
                stray._StrayCat__ws = websocket
                log.info(
                    f"New websocket connection for user '{user.id}', the old one has been closed."
                )
                return stray

            else:
                stray = StrayCat(
                    ws=websocket,
                    user_id=user.name, # TODOV2: user_id should be the user.id
                    user_data=user,
                    main_loop=asyncio.get_running_loop(),
                )
                strays[user.id] = stray
                return stray

        # extract credential from request
        credential = await ws_extract_credential(websocket)

        auth_handlers = [
            # try to get user from local idp
            websocket.app.state.ccat.core_auth_handler,
            # try to get user from auth_handler
            websocket.app.state.ccat.custom_auth_handler,
        ]
        for ah in auth_handlers:
            user: AuthUserInfo = await ah.authorize_user_from_credential(
                credential, resource, permission, user_id=user_id
            )
            if user:
                return await get_user_stray(user)

        raise WebSocketException(code=1004, reason="Invalid Credentials")

    return ws_auth


def http_auth(resource: AuthResource, permission: AuthPermission) -> Callable:
    async def http_auth(request: Request) -> None | StrayCat:
        """Authenticate endpoint.

        Parameters
        ----------
        request : Request
            HTTP request.

        Returns
        -------
        None
            Does not raise an exception if the request is allowed.

        Raises
        ------
        HTTPException
            Error with status code `403` if the request is not allowed.
        """

        async def get_user_stray(user: AuthUserInfo):
            strays = request.app.state.strays
            event_loop = request.app.state.event_loop

            if user.id not in strays.keys():
                strays[user.id] = StrayCat(
                     # TODOV2: user_id should be the user.id
                    user_id=user.name, user_data=user, main_loop=event_loop
                )
            return strays[user.id]

        # extract credential from request
        credential = await http_extract_credential(request)
        user_id = request.headers.get("user_id", "user")

        auth_handlers = [
            # try to get user from local idp
            request.app.state.ccat.core_auth_handler,
            # try to get user from auth_handler
            request.app.state.ccat.custom_auth_handler,
        ]
        for ah in auth_handlers:
            user: AuthUserInfo = await ah.authorize_user_from_credential(
                credential, resource, permission, user_id=user_id
            )
            if user:
                return await get_user_stray(user)

        raise HTTPException(status_code=403, detail={"error": "Invalid Credentials"})

    return http_auth


async def frontend_auth(request: Request) -> None | StrayCat:
    """Authenticate the admin panel and other core webapps / single page apps, with ccat_user_token cookie.

    Parameters
    ----------
    request : Request
        HTTP request.

    Returns
    -------
    None
        Does not raise an exception if the request is allowed.

    Raises
    ------
    HTTPException
        Redirects to login page.

    """

    token = request.cookies.get("ccat_user_token")
    if token:
        # decode token
        core_auth_handler = request.app.state.ccat.core_auth_handler
        user: AuthUserInfo = await core_auth_handler.authorize_user_from_jwt(
            token, AuthResource.ADMIN, AuthPermission.READ
        )

        if user:
            strays = request.app.state.strays
            event_loop = request.app.state.event_loop

            if user.id not in strays.keys():
                strays[user.id] = StrayCat(
                     # TODOV2: user_id should be the user.id
                    user_id=user.name, user_data=user, main_loop=event_loop
                )
            return strays[user.id]

    # no token or invalid token, redirect to login
    referer_query = urlencode({"referer": request.url.path})
    raise HTTPException(
        status_code=307,
        headers={
            "Location": f"/auth/login?{referer_query}"
            # TODOAUTH: cannot manage to make the Referer header to work
            # "Referer": request.url.path
        },
    )
