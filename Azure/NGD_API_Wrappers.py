import re
import os
import json
from datetime import datetime, timedelta

import requests as r

from shapely import from_wkt
from shapely.geometry import Point, LineString, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.errors import GEOSException

from azure.monitor.events.extension import track_event

UNIVERSAL_TIMEOUT = 20

def flatten_coords(list_of_lists: list) -> list:
    '''Flattens the coordinates of geojson features into a flattened list of coordinate pairs.'''
    result = []
    for item in list_of_lists:
        if isinstance(item[0], list):
            flattened = flatten_coords(item)
            result.extend(flattened)
        else:
            result.append(item)
    return result

def get_latest_collection_versions(flag_recent_updates: bool = True, recent_update_days: int = 31) -> dict:
    '''
    Returns the latest collection versions of each NGD collection.
    Feature collections follow the following naming convention: theme-collection-featuretype-version (eg. bld-fts-buildingline-2)
    The output of this function maps base feature collection names (theme-collection-featuretype) to the full name, including the latest version.
    This can be used to ensure that software is always using the latest version of a feature collection.
    More details on feature collection naming can be found at https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features/what-data-is-available
    '''

    response = r.get('https://api.os.uk/features/ngd/ofa/v1/collections/', timeout=UNIVERSAL_TIMEOUT)
    collections_data = response.json()['collections']
    collections_list = [collection['id'] for collection in collections_data]

    collections_dict = {}
    for col in collections_list:
        basename, version = re.split(r'-(?=[^-]*$)', col)
        version = int(version)
        if basename in collections_dict:
            collections_dict[basename].append(version)
        else:
            collections_dict[basename] = [version]

    output_lookup = {}
    for basename, versions in collections_dict.items():
        latest_version = max(versions)
        output_lookup[basename] = f'{basename}-{latest_version}'

    if not flag_recent_updates:
        return output_lookup

    recent_update_cutoff = datetime.now() - timedelta(days=recent_update_days)
    latest_versions_data = [c for c in collections_data if c['id'] in output_lookup.values()]
    recent_collections = []

    for collection_data in latest_versions_data:
        version_startdate = collection_data['extent']['temporal']['interval'][0][0]
        time_obj = datetime.strptime(version_startdate, r'%Y-%m-%dT%H:%M:%SZ')
        if time_obj > recent_update_cutoff:
            collection = collection_data['id']
            recent_collections.append(collection)

    full_output = {
        'collection-lookup': output_lookup,
        'recent-update-threshold-days': recent_update_days,
        'recent-collection-updates': recent_collections
    }

    return full_output

def get_specific_latest_collections(collection: list[str], **kwargs) -> str:
    '''
    Returns the latest collection(s) from the base name of given collection(s).
    Input must be a list in the format theme-collection-featuretype (eg. bld-fts-buildingline)
    Output will supply a dictionary completing the full name of the feature collections by appending the latest version number (eg. bld-fts-buildingline-2)
    More details on feature collection naming can be found at https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features/what-data-is-available
    '''
    latest_collections = get_latest_collection_versions(flag_recent_updates=False, **kwargs)
    try:
        specific_latest_collections = {col: latest_collections[col] for col in collection}
    except KeyError as e:
        return {
            "code": 404,
            "description": f"Collection {e} is not a supported Collection base name. The name must not include a version suffix. Please refer to the documentation for a list of supported Collections.",
            "help": "https://api.os.uk/features/ngd/ofa/v1/collections"
        }
                      
    return specific_latest_collections

def get_access_token(client_id: str, client_secret: str) -> str:
    '''
    Supplies a temporary access token for of the OS NGD API
    Times out after 5 minutes
    Takes the project client_id and client_secret as input
    '''

    url = "https://api.os.uk/oauth2/token/v1"

    data = {
        "grant_type": "client_credentials"
    }

    response = r.post(
        url,
        auth=(client_id, client_secret),
        data=data,
        timeout=UNIVERSAL_TIMEOUT
    )

    json_response = response.json()
    if response.status_code == 401:
        raise PermissionError(json_response)
    token = json_response["access_token"]

    return token

def oauth2_manager(func: callable) -> callable:
    '''
    A wrapper function, extending the input function to handle OAuth2 authentication using environment variables.
    '''

    def wrapper(
        *args,
        headers: dict = None,
        **kwargs
    ) -> dict:

        headers = headers.copy() if headers else {}
        kwargs_ = kwargs.copy()

        access_token = os.environ.get('ACCESS_TOKEN', '')
        headers['Authorization'] = f'Bearer {access_token}'
        response = func(*args, headers = headers, **kwargs_)
        if response.get('code') != 401:
            print('Using existing access token')
            return response
        client_id = os.environ.get('CLIENT_ID')
        client_secret = os.environ.get('CLIENT_SECRET')
        print('Recalculating access token')
        try:
            access_token = get_access_token(
                client_id=client_id,
                client_secret=client_secret
            )
        except PermissionError:
            return {
                "code": 401,
                "description": "Missing or invalid CLIENT_ID and/or CLIENT_SECRET. Make sure these are configured correctely in your environment variables.",
                "errorSource": "Catalyst Wrapper"
            }
        os.environ['ACCESS_TOKEN'] = access_token
        headers['Authorization'] = f'Bearer {access_token}'
        response = func(*args, headers = headers, **kwargs_)
        return response

    wrapper.__name__ = func.__name__ + '+OAuth2_manager'
    funcname = func.__name__
    wrapper.__doc__ = f'''
    An extension of the function {funcname} handling oauth2 authorisation.
    IMPORTANT:
        CLIENT_ID and CLIENT_SECRET must be set as environment variables for this extension to work.
        This can be done using a .env file and load_dotenv()
    Docs for OAuth2 with Ordnance Survey data can be found at https://osdatahub.os.uk/docs/oauth2/overview
    The function automatically ensures a valid temporary access token (expiring after 5 minutes) is being used. This will either be an existing valid token, or a newly called one.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    '''
    return wrapper

def wkt_to_spatial_filter(wkt: str, predicate: str = 'INTERSECTS') -> str:
    '''Constructs a full spatial filter in conformance with the OGC API - Features standard from well-known-text (wkt)
    Currently, only 'Simple CQL' conformance is supported, therefore INTERSECTS is the only supported spatial predicate: https://portal.ogc.org/files/96288#rc_simple-cql'''
    return f'({predicate}(geometry,{wkt}))'

def construct_bbox_filter(
        bbox_tuple: tuple[float | int] | str = None,
        xmin: float | int = None,
        ymin: float | int = None,
        xmax: float | int = None,
        ymax: float | int = None
) -> str:
    '''Constructs a bounding box filter for an API query.'''
    if bbox_tuple:
        return str(bbox_tuple)[1:-1].replace(' ','')
    list_ = []
    for z in [xmin, ymin, xmax, ymax]:
        if z is None:
            raise AttributeError('You must provide either bbox_tuple or all of [xmin, ymin, xmax, ymax]')
        list_.append(str(z))
    if xmin > xmax:
        raise ValueError('xmax must be greater than xmin')
    if ymin > ymax:
        raise ValueError('ymax must be greater than ymin')
    return ','.join(list_)

def construct_filter_param(**params) -> str:
    '''Constructs a set of key=value parameters into a filter string for an API query'''
    for k, v in params.items():
        if isinstance(str, v):
            params[k] = f"'{v}'"
    filter_list = [f"({k}={v})" for k, v in params.items()]
    return 'and'.join(filter_list)

def ngd_items_request(
    collection: str,
    query_params: dict = None,
    filter_params: dict = None,
    wkt = None,
    use_latest_collection: bool = False,
    add_metadata: bool = True,
    headers: dict = None,
    **kwargs
) -> dict:
    '''
    Calls items from the OS NGD API - Features
        - https://osdatahub.os.uk/docs/wfs/overview
        - https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features
    Parameters:
        collection (str) - the feature collection to call from. Feature collection names and details can be found at https://api.os.uk/features/ngd/ofa/v1/collections/
        query_params (dict, optional) - parameters to pass to the query as query parameters, supplied in a dictionary. Supported parameters are: bbox, bbox-crs, crs, datetime, filter, filter-crs, filter-lang, limit, offset
        filter_params (dict, optional) - OS NGD attribute filters to pass to the query within the 'filter' query_param. The can be used instead of or in addition to manually setting the filter in query_params.
            The key-value pairs will appended using the EQUAL TO [ = ] comparator. Any other CQL Operator comparisons must be set manually in query_params.
            Queryable attributes can be found in OS NGD codelists documentation https://docs.os.uk/osngd/code-lists/code-lists-overview, or by inserting the relevant collectionId into the https://api.os.uk/features/ngd/ofa/v1/collections/{{collectionId}}/queryables endpoint.
        wkt (string or shapely geometry object) - A means of searching a geometry for features. The search area(s) must be supplied in wkt, either in a string or as a Shapely geometry object.
            The function automatically composes the full INTERSECTS filter and adds it to the 'filter' query parameter.
            Make sure that 'filter-crs' is set to the appropriate value.
        use_latest_collection (boolean, default False) - If True, it ensures that if a specific version of a collection is not supplied (eg. bld-fts-building[-2]), the latest version is used.
            Note that if use_latest_collection but 'collection' does specify a version, the specified version is always used regardless of use_latest_collection.
        headers (dict, optional) - Headers to pass to the query. These can include bearer-token authentication.
        access_token (str) - An access token, which will be added as bearer token to the headers.
        **kwargs: other generic parameters to be passed to the requests.get()

    Returns the features as a geojson, as per the OS NGD API.
    '''

    query_params = query_params.copy() if query_params else {}
    filter_params = filter_params.copy() if filter_params else {}
    headers = headers.copy() if headers else {}

    kwargs.pop('hierarchical_output', None)

    if use_latest_collection:
        collection = get_specific_latest_collections([collection]).get(collection)

    if filter_params:
        filters = construct_filter_param(**filter_params)
        current_filters = query_params.get('filter')
        query_params['filter'] = f'({current_filters})and{filters}' if current_filters else filters

    if wkt:
        spatial_filter = wkt_to_spatial_filter(wkt)
        current_filters = query_params.get('filter')
        query_params['filter'] = f'({current_filters})and{spatial_filter}' if current_filters else spatial_filter

    for k, v in query_params.items():
        if 'crs' in k and isinstance(v, int):
            query_params[k] = f'http://www.opengis.net/def/crs/EPSG/0/{v}'
    url = f'https://api.os.uk/features/ngd/ofa/v1/collections/{collection}/items/'

    # Remove host header as this is automatically added by the requests library and can cause issues
    headers.pop('host', None)
    response = r.get(
        url,
        params=query_params,
        headers=headers,
        timeout=UNIVERSAL_TIMEOUT,
        **kwargs
    )

    status_code = response.status_code

    try:
        json_response = response.json()
    except json.JSONDecodeError as e:
        error_string = str(e)
        if error_string.startswith('Expecting value'):
            status_code = 414
            error_string = {
                'Error Text': error_string,
                'Help (Catalyst)': 'This could be due to a request URI which is too long or an input geometry which is too complex.'
            }
        return {
            "code": status_code,
            "description": error_string,
            "errorSource": "OS NGD API"
        }

    if status_code >= 400:
        descr = json_response.get('description', '')
        if not descr:
            json_response.pop('code', None)
            descr = json_response
            json_response = {'code': status_code, 'description': descr}
        elif descr.startswith('Not supported query parameter'):
            descr = descr.replace('Supported parameters are', 'Supported NGD parameters are')
            descr += '. Additional supported Catalyst parameters for this function are: {attr}.'
            json_response['description'] = descr
        if not json_response.get('code'):
            json_response = {'code': status_code} | json_response
        json_response['errorSource'] = 'OS NGD API'
        return json_response

    compiled_features = [feature['geometry']['coordinates'] for feature in json_response['features']]
    flattened_coords = flatten_coords(compiled_features)
    xcoords, ycoords = [], []
    for pair in flattened_coords:
        xcoords.append(pair[0])
        ycoords.append(pair[1])
    bbox = (min(xcoords), min(ycoords), max(xcoords), max(ycoords)) if xcoords and ycoords else ''
    custom_dimensions = {
        'method': 'GET',
        'url.path': url,
        'url.path_params.collection': collection,
        'response.bbox': bbox,
        'response.numberReturned': json_response['numberReturned'],
    }
    str_query_params = {f'url.query_params.{str(k)}': str(v) for k, v in query_params.items()}
    custom_dimensions.update(str_query_params)
    track_event('OS NGD API - Features', custom_dimensions=custom_dimensions)

    for feature in json_response['features']:
        feature['collection'] = collection
        feature['properties']['collection'] = collection

    if add_metadata:
        json_response['numberOfRequests'] = 1
    return json_response

def limit_extension(func: callable) -> callable:
    '''
    A wrapper function, extending the input function to handle pagination from OS NGD API - Features. 
    '''

    def wrapper(
        *args,
        request_limit: int = 50,
        limit: int = None,
        query_params: dict = None,
        **kwargs
    ) -> dict:

        query_params = query_params.copy() if query_params else {}

        if 'offset' in query_params:
            return {
                "code": 400,
                "description": "'offset' is not a valid attribute for functions using this Catalyst wrapper.",
                "errorSource": "Catalyst Wrapper"
            }

        features = []

        batch_count, final_batchsize = divmod(limit, 100) if limit else (None, None)
        request_count = 0
        offset = 0

        if not limit and not request_limit:
            raise AttributeError('At least one of limit or request_limit must be provided to prevent indefinitely numerous requests and high costs. However, there is no upper limit to these values.')

        while (request_count != request_limit) and (not(limit) or offset < limit):

            if request_count == batch_count:
                query_params['limit'] = final_batchsize
            query_params['offset'] = offset

            json_response = func(
                *args,
                query_params = query_params,
                add_metadata = False,
                **kwargs
            )
            if json_response.get('code') and json_response['code'] >= 400:
                return json_response
            request_count += 1
            features += json_response['features']

            if not [link for link in json_response['links'] if link['rel'] == 'next']:
                break

            offset += 100

        geojson = {
            "type": "FeatureCollection",
            "numberOfRequests": request_count,
            "numberReturned": len(features),
            "timeStamp": datetime.now().isoformat(),
            "collection": kwargs.get('collection'),
            "features": features
        }
        return geojson

    wrapper.__name__ = func.__name__ + '+limit_extension'
    funcname = func.__name__
    wrapper.__doc__ = f'''
    This is an extension the {funcname} function, which returns OS NGD features.
    It serves to extend the maximum number of features returned above the default maximum 100 by looping through multiple requests.
    It takes the following arguments:
    - collection: The name of the collection to be queried.
    - request_limit: The maximum number of calls to be made to {funcname}. Default is 50.
    - limit: The maximum number of features to be returned. Default is None.
    - query_params: A dictionary of query parameters to be passed to the function. Default is an empty dictionary.
    To prevent indefinite requests and high costs, at least one of limit or request_limit must be provided, although there is no limit to the upper value these can be.
    It will make multiple requests to the function to compile all features from the specified collection, returning a dictionary with the features and metadata.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    '''
    return wrapper

def multilevel_explode(shape: BaseGeometry) -> list[Polygon | LineString | Point]:
    '''
    Explode a geometry into its constituent parts.
    Where multigeometries contain other multigeometries, the layers are flattened into a single list, such that the results lists contains only single geomtries.
    '''

    if isinstance(shape, (Point, LineString, Polygon)):
        return [shape]

    lower_shapes = shape.geoms
    result_list = []
    for lshape in lower_shapes:
        lower_shape_exploded = multilevel_explode(lshape)
        result_list.extend(lower_shape_exploded)
    return result_list

def multigeometry_search_extension(func: callable) -> callable:
    '''
    A wrapper function, extending the input function handle multigeometry search areas, searching each one in turn.
    '''

    def flatten_search_areas(search_areas: list) -> dict:
        '''
        Flattens hierarchical search area results into a single geojson object, merging appropriate metadata.
        '''

        geojson = {
            'type': 'FeatureCollection',
            'numberOfRequests': 0,
            'numberReturned': 0,
            'features': []
        }

        ids = []
        geojson_fts = geojson['features']

        for area in search_areas:

            search_area_number = area.pop('searchAreaNumber')

            features = area['features']
            for feat in features:
                feat['searchAreaNumber'] = search_area_number
                feat['properties']['searchAreaNumber'] = search_area_number

            new_features = []
            for feat in features:
                if feat['id'] in ids:
                    index = [v for v, gf in enumerate(geojson_fts) if gf['id'] == feat['id']][0]
                    n = geojson_fts[index]['searchAreaNumber']
                    n = [n] if not(isinstance(n, list)) else n
                    n.append(search_area_number)
                    geojson_fts[index]['searchAreaNumber'] = n
                else:
                    feat['searchAreaNumber'] = search_area_number
                    new_features.append(feat)
                    ids.append(feat['id'])

            geojson_fts += new_features
            geojson['numberOfRequests'] += area['numberOfRequests']
            geojson['numberReturned'] += len(new_features)

        geojson['timeStamp'] = datetime.now().isoformat()

        return geojson
    
    def wrapper(
        *args,
        wkt: str,
        hierarchical_output: bool = False,
        **kwargs
    ) -> dict:

        try:
            full_geom = from_wkt(wkt) if isinstance(wkt, str) else wkt
        except GEOSException:
            return {
                "code": 400,
                "description": "The input geometry is not valid. Please ensure you have the correct formatting for your input geometry type.",
                "help": "http://libgeos.org/specifications/wkt/",
                "errorSource": "Catalyst Wrapper"
            }

        search_areas = []
        partial_geoms = multilevel_explode(full_geom)

        for search_area, geom in enumerate(partial_geoms):
            json_response = func(
                *args,
                wkt=geom,
                **kwargs
            )
            if json_response.get('code') and json_response['code'] >= 400:
                return json_response
            json_response['searchAreaNumber'] = search_area
            search_areas.append(json_response)

        if hierarchical_output:
            response = {
                "searchAreas": search_areas
            }
            return response

        response = flatten_search_areas(search_areas)

        return response

    wrapper.__name__ = func.__name__ + '+multigeometry_search_extension'
    funcname = func.__name__
    wrapper.__doc__ = f'''
    An alternative means of returning OS NGD features for a search area which is a Multi-Geometry (MultiPoint, MultiLinestring, or MultiPolygon), which will in some cases improve speed, performance, and prevent the call from timing out.
    Extends to {funcname} function.
    Each component shape of the multi-geometry will be searched in turn using the {funcname} function.
    The results are returned in a quasi-GeoJSON format, with features returned under 'searchAreas' in a list, where each item is a json object of results from one search area.
    The search areas are labelled numerically, with the number stored under 'searchAreaNumber'.
    NOTE: If a limit is supplied for the maximum number of features to be returned or requests to be made, this will apply to each search area individually, not to the overall number of results.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    '''
    return wrapper

def multiple_collections_extension(func: callable) -> dict:
    '''
    A wrapper function, extending the input function handle multiple OS collections as inputs.
    '''

    def apply_latest_collection(collection: str) -> list[str]:
        '''
        Applies the latest collection version to a list of collections.
        Takes a list of collection names as input, and returns a list of the latest version of each collection.
        If a collection name is supplied with a version suffix, this will be used instead of the latest version.
        '''
        has_version, no_version = [], []
        for c in collection:
            if c[-1].isdigit():
                has_version.append(c)
            else:
                no_version.append(c)
        new_collection = list(get_specific_latest_collections(no_version).values())
        new_collection.extend(has_version)
        return new_collection

    def wrapper(
        collection: list[str],
        *args,
        hierarchical_output: bool = False,
        use_latest_collection: bool = False,
        **kwargs
    ) -> dict:

        if use_latest_collection:
            collection = apply_latest_collection(collection)

        results = {}
        for col in collection:
            json_response = func(
                *args,
                collection=col,
                hierarchical_output=hierarchical_output,
                **kwargs
            )
            code = json_response.get('code', 200)
            if code == 404 and 'is not a supported Collection' in json_response.get('description'):
                return json_response
            if code >= 400:
                return json_response
            results[col] = json_response

        if hierarchical_output:
            return results

        geojson = {
            'type': 'FeatureCollection',
            'numberOfRequests': 0,
            'numberOfRequestsByCollection': {},
            'numberReturned': 0,
            'numberReturnedByCollection': {},
            'features': []
        }

        for col, col_results in results.items():

            features = col_results['features']
            geojson['features'] += features
            number_of_requests = col_results.pop('numberOfRequests')
            geojson['numberOfRequests'] += number_of_requests
            geojson['numberOfRequestsByCollection'][col] = number_of_requests
            number_returned = col_results.pop('numberReturned')
            geojson['numberReturned'] += number_returned
            geojson['numberReturnedByCollection'][col] = number_returned

        geojson['timeStamp'] = datetime.now().isoformat()

        return geojson

    wrapper.__name__ = func.__name__ + '+multiple_collections_extension'
    funcname = func.__name__
    wrapper.__doc__ = f'''
    Extents the {funcname} function to handle multiple collections.
    Takes a list of collection names as input, alongside any other parameters which are passed to {funcname}.
    The function {funcname} will be run for each collection in turn, with the results returned in a dictionary mapping the collection names to the results.
    NOTE: If a limit is supplied for the maximum number of features to be returned or requests to be made, this will apply to each collection individually, not to the overall number of results.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    '''
    return wrapper

# All possible ways of combining different wrappers in combos with OAuth2

items = ngd_items_request
items_auth = oauth2_manager(items)

items_limit = limit_extension(items)
items_geom = multigeometry_search_extension(items)
items_col = multiple_collections_extension(items)
items_limit_geom = multigeometry_search_extension(items_limit)
items_limit_col = multiple_collections_extension(items_limit)
items_geom_col = multiple_collections_extension(items_geom)
items_limit_geom_col = multiple_collections_extension(items_limit_geom)

items_auth_limit = limit_extension(items_auth)
items_auth_geom = multigeometry_search_extension(items_auth)
items_auth_col = multiple_collections_extension(items_auth)
items_auth_limit_geom = multigeometry_search_extension(items_auth_limit)
items_auth_limit_col = multiple_collections_extension(items_auth_limit)
items_auth_geom_col = multiple_collections_extension(items_auth_geom)
items_auth_limit_geom_col = multiple_collections_extension(items_auth_limit_geom)
