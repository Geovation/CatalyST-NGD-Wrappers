import requests as r
import re
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from shapely import from_wkt
from shapely.geometry import Point, LineString, Polygon, MultiPolygon
from collections import defaultdict

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
        data=data
    )

    json_response = response.json()
    if response.status_code >= 400:
        raise Exception(json_response)
    token = json_response["access_token"]

    return token

def OAauth2_manager(func: callable):

    def wrapper(*args, **kwargs):

        kwargs_ = kwargs.copy()

        try:
            access_token = os.environ.get('ACCESS_TOKEN')
            kwargs_['access_token'] = access_token
            return func(*args, **kwargs_)
        except Exception:
            client_id = os.environ.get('CLIENT_ID')
            client_secret = os.environ.get('CLIENT_SECRET')
            access_token = get_access_token(
                client_id=client_id,
                client_secret=client_secret
            )
            os.environ['ACCESS_TOKEN'] = access_token
            kwargs_['access_token'] = access_token
            return func(*args, **kwargs_)

    wrapper.__name__ = func.__name__ + '+OAuth2_manager'
    funcname = func.__name__
    wrapper.__doc__ = f"""
    An extension of the function {funcname} handling OAauth2 authorisation.
    IMPORTANT:
        CLIENT_ID and CLIENT_SECRET must be set as environment variables for this extension to work.
        This can be done using a .env file and load_dotenv()
    Docs for OAuth2 with Ordnance Survey data can be found at https://osdatahub.os.uk/docs/oauth2/overview
    The function automatically ensures a valid temporary access token (expiring after 5 minutes) is being used. This will either be an existing valid token, or a newly called one.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    """
    return wrapper

def wkt_to_spatial_filter(wkt, predicate='INTERSECTS'):
    '''Constructs a full spatial filter in conformance with the OGC API - Features standard from well-known-text (wkt)
    Currently, only 'Simple CQL' conformance is supported, therefore INTERSECTS is the only supported spatial predicate: https://portal.ogc.org/files/96288#rc_simple-cql'''
    return f'({predicate}(geometry,{wkt}))'

def construct_bbox_filter(
        bbox_tuple: tuple[float | int] | str = None,
        xmin: float | int = None,
        ymin: float | int = None,
        xmax: float | int = None,
        ymax: float | int = None
):
    if bbox_tuple:
        return str(bbox_tuple)[1:-1].replace(' ','')
    list_ = list()
    for z in [xmin, ymin, xmax, ymax]:
        if z == None:
            raise AttributeError('You must provide either bbox_tuple or all of [xmin, ymin, xmax, ymax]')
        list_.append(str(z))
    if xmin > xmax:
        raise ValueError('xmax must be greater than xmin')
    if ymin > ymax:
        raise ValueError('ymax must be greater than ymin')
    return ','.join(list_)

def construct_query_params(**params) -> str:
    '''
    Constructs a query string from a dictionary of key-value pairs.
    Refer to https://osdatahub.os.uk/docs/ofa/technicalSpecification for details about query parameters.
    The options are:
        - bbox
        - bbox-crs
        - crs
        - datetime
        - filter
        - filter-crs (can be supplied as a full http ref, or an integer)
        - filter-lang
        - limit
        - offset
    '''
    for p in ['crs', 'bbox-crs', 'filter-crs']:
        crs = params.get(p)
        if type(crs) == int:
            params[p] = f'http://www.opengis.net/def/crs/EPSG/0/{crs}'
    params_list = [f'{k}={v}' for k, v in params.items()]
    return '?' + '&'.join(params_list)

def construct_filter_param(**params):
    '''Constructs a set of key=value parameters into a filter string for an API query'''
    for k, v in params.items():
        if type(v) == str:
            params[k] = f"'{v}'"
    filter_list = [f"({k}={v})" for k, v in params.items()]
    return 'and'.join(filter_list)

def ngd_items_request(
    collection: str,
    query_params: dict = {},
    filter_params: dict = {},
    filter_wkt = None,
    headers: dict = {},
    access_token: str = None,
    add_metadata: bool = True,
    **kwargs
) -> dict:
    """
    Calls items from the OS NGD API - Features
        - https://osdatahub.os.uk/docs/wfs/overview
        - https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features
    Parameters:
        collection (str) - the feature collection to call from. Feature collection names and details can be found at https://api.os.uk/features/ngd/ofa/v1/collections/
        query_params (dict, optional) - parameters to pass to the query as query parameters, supplied in a dictionary. Supported parameters are: bbox, bbox-crs, crs, datetime, filter, filter-crs, filter-lang, limit, offset
        filter_params (dict, optional) - OS NGD attribute filters to pass to the query within the 'filter' query_param. The can be used instead of or in addition to manually setting the filter in query_params.
            The key-value pairs will appended using the EQUAL TO [ = ] comparator. Any other CQL Operator comparisons must be set manually in query_params.
            Queryable attributes can be found in OS NGD codelists documentation https://docs.os.uk/osngd/code-lists/code-lists-overview, or by inserting the relevant collectionId into the https://api.os.uk/features/ngd/ofa/v1/collections/{{collectionId}}/queryables endpoint.
        filter_wkt (string or shapely geometry object) - A means of searching a geometry for features. The search area(s) must be supplied in wkt, either in a string or as a Shapely geometry object.
            The function automatically composes the full INTERSECTS filter and adds it to the 'filter' query parameter.
            Make sure that 'filter-crs' is set to the appropriate value.
        headers (dict, optional) - Headers to pass to the query. These can include bearer-token authentication.
        access_token (str) - An access token, which will be added as bearer token to the headers.
        **kwargs: other generic parameters to be passed to the requests.get()

    Returns the features as a geojson, as per the OS NGD API.
    """

    query_params_ = query_params.copy()
    filter_params_ = filter_params.copy()
    headers_ = headers.copy()

    if filter_params_:
        filters = construct_filter_param(**filter_params_)
        current_filters = query_params_.get('filter')
        query_params_['filter'] = f'({current_filters})and{filters}' if current_filters else filters

    if filter_wkt:
        spatial_filter = wkt_to_spatial_filter(filter_wkt)
        current_filters = query_params_.get('filter')
        query_params_['filter'] = f'({current_filters})and{spatial_filter}' if current_filters else spatial_filter

    query_params_string = construct_query_params(**query_params_)
    url = f'https://api.os.uk/features/ngd/ofa/v1/collections/{collection}/items/{query_params_string}'
    if access_token:
        headers_['Authorization'] = f"Bearer {access_token}"
    response = r.get(url, headers=headers_, **kwargs)
    json_response = response.json()

    if response.status_code >= 400:
        raise Exception(json_response)

    for feature in json_response['features']:
        feature['collection'] = collection
        feature['properties']['collection'] = collection

    if add_metadata:
        json_response['source'] = "Compiled from code by Geovation from Ordnance Survey"
        json_response['numberOfRequests'] = 1

    return json_response

def limit_extension(func: callable):

    def wrapper(
        *args,
        request_limit: int = 50,
        limit: int = None,
        query_params: dict = {},
        **kwargs
    ):

        query_params_ = query_params.copy()

        if 'offset' in query_params_:
            raise AttributeError('offset is not a valid argument for functions using this decorator.')

        items = list()

        batch_count, final_batchsize = divmod(limit, 100) if limit else (None, None)
        request_count = 0
        offset = 0

        if not(limit) and not(request_limit):
            raise AttributeError('At least one of limit or request_limit must be provided to prevent indefinitely numerous requests and high costs. However, there is no upper limit to these values.')

        while (request_count != request_limit) and (not(limit) or offset < limit):

            if request_count == batch_count:
                print('final batch of size', final_batchsize)
                query_params_['limit'] = final_batchsize
            query_params_['offset'] = offset

            json_response = func(*args, query_params=query_params_, add_metadata = False, **kwargs)
            request_count += 1
            items += json_response['features']

            if not [link for link in json_response['links'] if link['rel'] == 'next']:
                break
            
            offset += 100

        geojson = {
            "type": "FeatureCollection",
            "numberOfRequests": request_count,
            "numberReturned": len(items),
            "timeStamp": datetime.now().isoformat(),
            "collection": kwargs.get('collection'),
            "source": "Compiled from code by Geovation from Ordnance Survey",
            "features": items
        }
        return geojson

    wrapper.__name__ = func.__name__ + '+limit_extension'
    funcname = func.__name__
    wrapper.__doc__ = f"""
    This is an extension the {funcname} function, which returns OS NGD features. It serves to extend the maximum number of features returned above the default maximum 100 by looping through multiple requests.
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
    """
    return wrapper

def multigeometry_search_extension(func: callable):

    def wrapper(*args, filter_wkt: str, heirarchical_output: bool = False, **kwargs):

        full_geom = from_wkt(filter_wkt) if type(filter_wkt) == str else filter_wkt
        search_areas = list()

        is_single_geom = type(full_geom) in [Point, LineString, Polygon]
        partial_geoms = [full_geom] if is_single_geom else full_geom.geoms

        for search_area, geom in enumerate(partial_geoms):
            json_response = func(*args, filter_wkt=geom, **kwargs)
            json_response['searchAreaNumber'] = search_area
            search_areas.append(json_response)

        if heirarchical_output:
            response = {
                "searchAreas": search_areas
            }
            return response

        geojson = {
            'type': 'FeatureCollection',
            'source': 'Compiled from code by Geovation from Ordnance Survey',
            'numberOfRequests': 0,
            'numberReturned': 0,
            'features': []
        }

        ids = list()
        geojson_fts = geojson['features']
        for area in search_areas:

            searchAreaNumber = area.pop('searchAreaNumber')

            features = area['features']
            for feature in features:
                feature['searchAreaNumber'] = searchAreaNumber

            new_features = list()
            for f in features:
                if f['id'] in ids:
                    print(f['id'])
                    index = [v for v, gf in enumerate(geojson_fts) if gf['id'] == f['id']][0]
                    n = geojson_fts[index]['searchAreaNumber']
                    n = [n] if type(n) != list else n
                    n.append(searchAreaNumber)
                    geojson_fts[index]['searchAreaNumber'] = n
                else:
                    f['searchAreaNumber'] = searchAreaNumber
                    new_features.append(f)
                    ids.append(f['id'])

            geojson_fts += new_features
            geojson['numberOfRequests'] += area['numberOfRequests']
            geojson['numberReturned'] += len(new_features)
        
        geojson['timeStamp'] = datetime.now().isoformat()

        return geojson

    wrapper.__name__ = func.__name__ + '+multigeometry_search_extension'
    funcname = func.__name__
    wrapper.__doc__ = f"""
    An alternative means of returning OS NGD features for a search area which is a Multi-Geometry (MultiPoint, MultiLinestring, or MultiPolygon), which will in some cases improve speed, performance, and prevent the call from timing out.
    Extends to {funcname} function.
    Each component shape of the multi-geometry will be searched in turn using the {funcname} function.
    The results are returned in a quasi-GeoJSON format, with features returned under 'searchAreas' in a list, where each item is a json object of results from one search area.
    The search areas are labelled numerically, with the number stored under 'searchAreaNumber'.
    NOTE: If a limit is supplied for the maximum number of features to be returned or requests to be made, this will apply to each search area individually, not to the overall number of results.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    """
    return wrapper

def multiple_collections_extension(func: callable) -> dict:

    def wrapper(collection: list[str], heirarchical_output: bool = False, *args, **kwargs):

        results = dict()
        for c in collection:
            json_response = func(c, heirarchical_output=heirarchical_output, *args, **kwargs)
            results[c] = json_response
        
        if heirarchical_output:
            return results
    
        geojson = {
            'type': 'FeatureCollection',
            'source': 'Compiled from code by Geovation from Ordnance Survey',
            'numberOfRequests': 0,
            'numberOfRequestsByCollection': {},
            'numberReturned': 0,
            'numberReturnedByCollection': {},
            'features': []
        }

        for collection, collection_results in results.items():

            collection_results.pop('timeStamp')
            features = collection_results['features']
            geojson['features'] += features
            numberOfRequests = collection_results.pop('numberOfRequests')
            geojson['numberOfRequests'] += numberOfRequests
            geojson['numberOfRequestsByCollection'][collection] = numberOfRequests
            numberReturned = collection_results.pop('numberReturned')
            geojson['numberReturned'] += numberReturned
            geojson['numberReturnedByCollection'][collection] = numberReturned
        
        geojson['timeStamp'] = datetime.now().isoformat()

        return geojson
    
    wrapper.__name__ = func.__name__ + '+multiple_collections_extension'
    funcname = func.__name__
    wrapper.__doc__ = f"""
    Extents the {funcname} function to handle multiple collections.
    Takes a list of collection names as input, alongside any other parameters which are passed to {funcname}.
    The function {funcname} will be run for each collection in turn, with the results returned in a dictionary mapping the collection names to the results.
    NOTE: If a limit is supplied for the maximum number of features to be returned or requests to be made, this will apply to each collection individually, not to the overall number of results.

    ____________________________________________________
    Docs for {funcname}:
        {func.__doc__}
    """
    return wrapper

def get_latest_collection_versions(flag_recent_updates: bool = True, recent_update_days: int = 31) -> tuple[dict[str: str], list[str]]:
    '''
    Returns the latest collection versions of each NGD collection.
    Feature collections follow the following naming convention: theme-collection-featuretype-version (eg. bld-fts-buildingline-2)
    The output of this function maps base feature collection names (theme-collection-featuretype) to the full name, including the latest version.
    This can be used to ensure that software is always using the latest version of a feature collection.
    More details on feature collection naming can be found at https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features/what-data-is-available
    '''

    response = r.get('https://api.os.uk/features/ngd/ofa/v1/collections/')
    collections_data = response.json()['collections']
    collections_list = [collection['id'] for collection in collections_data]
    collection_base_names = set([re.sub(r'-\d+$', '', c) for c in collections_list])
    output_lookup = dict()

    for base_name in collection_base_names:
        all_versions = [c for c in collections_list if c.startswith(base_name)]
        latest_version = max(all_versions, key=lambda c: int(c.split('-')[-1]))
        output_lookup[base_name] = latest_version

    recent_collections = None
    if flag_recent_updates:
        time_format = r'%Y-%m-%dT%H:%M:%SZ'
        recent_update_cutoff = datetime.now() - timedelta(days=recent_update_days)
        latest_versions_data = [c for c in collections_data if c['id'] in output_lookup.values()]
        recent_collections = list()
        for collection_data in latest_versions_data:
            version_startdate = collection_data['extent']['temporal']['interval'][0][0]
            time_obj = datetime.strptime(version_startdate, time_format)
            if time_obj > recent_update_cutoff:
                collection = collection_data['id']
                recent_collections.append(collection)
                logging.warning(f'{collection} is a recent version/update from the last {recent_update_days} days.')

    return output_lookup, recent_collections

def get_single_latest_collection(collection: str, **kwargs) -> str:
    '''
    Returns the latest collection of a given collection base.
    Input must be in the format theme-collection-featuretype (eg. bld-fts-buildingline)
    Output will complete the full name of the feature collection by appending the latest version number (eg. bld-fts-buildingline-2)
    More details on feature collection naming can be found at https://docs.os.uk/osngd/accessing-os-ngd/access-the-os-ngd-api/os-ngd-api-features/what-data-is-available
    '''
    latest_collections = get_latest_collection_versions(**kwargs)
    latest_collection = latest_collections[collection]
    return latest_collection

# All possible ways of combining different wrappers in combos with OAuth2

items_auth = OAauth2_manager(ngd_items_request)
items_auth_limit = limit_extension(items_auth)
items_auth_limit_geom = multigeometry_search_extension(items_auth_limit)
items_auth_limit_geom_col = multiple_collections_extension(items_auth_limit_geom)

items_auth_geom = multigeometry_search_extension(items_auth)
items_auth_geom_col = multiple_collections_extension(items_auth_geom)

items_auth_limit_col = multiple_collections_extension(items_auth_limit)

items_auth_col = multiple_collections_extension(items_auth)