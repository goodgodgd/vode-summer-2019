import tensorflow as tf
import numpy as np
import cv2


def load_data():
    srcidx = 1430
    tgtidx = 1450
    src_image = cv2.imread(f"samples/color/{srcidx:05d}.jpg")
    tgt_image = cv2.imread(f"samples/color/{tgtidx:05d}.jpg")
    tgt_depth = cv2.imread(f"samples/depth/{tgtidx:05d}.png", cv2.IMREAD_ANYDEPTH)
    tgt_depth = tgt_depth/1000.

    src_pose = np.loadtxt(f"samples/pose/pose_{srcidx:05d}.txt")
    tgt_pose = np.loadtxt(f"samples/pose/pose_{tgtidx:05d}.txt")

    intrinsic = np.loadtxt("samples/intrinsic.txt")
    t2s_pose = np.matmul(np.linalg.inv(src_pose), tgt_pose)
    print("target to source pose matrix", t2s_pose)
    # t2s_pose = np.identity(4)
    print(f"loaded data shapes: src img={src_image.shape}, tgt img={tgt_image.shape}, "
          f"depth={tgt_depth.shape}, {tgt_depth.dtype}, pose={t2s_pose.shape}, "
          f"src intrinsic={intrinsic.shape}")

    src_image = tf.constant(src_image)
    tgt_image = tf.constant(tgt_image)
    tgt_depth = tf.constant(tgt_depth)
    t2s_pose = tf.constant(t2s_pose)
    intrinsic = tf.constant(intrinsic)
    return src_image, tgt_image, tgt_depth, t2s_pose, intrinsic


def synthesize_view(src_image, tgt_depth, pose, intrinsic):
    """
    synthesize target view images from source view image
    :param src_image: source image nearby the target image [height, width, 3]
    :param tgt_depth: depth map of the target image in meter scale [height, width]
    :param pose: (target to source) camera pose matrix [4, 4]
    :param intrinsic: camera intrinsic parameters [3, 3]
    :return: synthesized target view image [height, width, 3]
    """
    height, width, _ = src_image.get_shape().as_list()
    debug_coords = pixel_meshgrid(height, width, 80)
    debug_inds = tf.cast(debug_coords[0, 16:-16] + debug_coords[1, 16:-16]*width, tf.int32)
    tgt_pixel_coords = pixel_meshgrid(height, width)
    print("pixel coordintes", tf.gather(tgt_pixel_coords[:2, :], debug_inds, axis=1))

    tgt_cam_coords = pixel2cam(tgt_pixel_coords, tgt_depth, intrinsic)
    src_cam_coords = transform_to_source(tgt_cam_coords, pose)
    debug_cam_coords = tf.transpose(tf.concat((tgt_cam_coords[:3, :], src_cam_coords[:3, :]), axis=0))
    print("tgt src points compare", tf.gather(debug_cam_coords, debug_inds))

    src_pixel_coords = cam2pixel(src_cam_coords, intrinsic)
    print("src_pixel_coords", tf.gather(src_pixel_coords[:2, :], debug_inds, axis=1))

    tgt_image_synthesized = reconstruct_image_roundup(src_pixel_coords, src_image, tgt_depth)
    print("reconstructed image", tgt_image_synthesized.get_shape(), tgt_image_synthesized.dtype)
    tgt_image_synthesized = tgt_image_synthesized.numpy()
    return tgt_image_synthesized


def pixel_meshgrid(height, width, stride=1):
    """
    :return: pixel coordinates like vectors of (u,v,1) [3, height*width]
    """
    v = np.linspace(0, height-stride, int(height//stride))
    u = np.linspace(0, width-stride,  int(width//stride))
    ugrid, vgrid = tf.meshgrid(u, v)
    uv = tf.stack([ugrid, vgrid], axis=0)
    uv = tf.reshape(uv, (2, -1))
    num_pts = uv.get_shape().as_list()[1]
    uv = tf.concat([uv, tf.ones((1, num_pts), tf.float64)], axis=0)
    return uv


def pixel2cam(pixel_coords, depth, intrinsic):
    """
    :param pixel_coords: (u,v,1) [3, height*width]
    :param depth: [height, width]
    :param intrinsic: [3, 3]
    :return: 3D points like (x,y,z,1) in target frame [4, height*width]
    """
    depth = tf.reshape(depth, (1, -1))
    cam_coords = tf.matmul(tf.linalg.inv(intrinsic), pixel_coords)
    cam_coords *= depth
    num_pts = cam_coords.get_shape().as_list()[1]
    cam_coords = tf.concat([cam_coords, tf.ones((1, num_pts), tf.float64)], axis=0)
    return cam_coords


def transform_to_source(tgt_coords, t2s_pose):
    """
    :param tgt_coords: target frame coordinates like (x,y,z,1) [4, height*width]
    :param t2s_pose: 4x4 pose matrix to transform points from target frame to source frame
    :return: transformed points in source frame like (x,y,z,1) [4, height*width]
    """
    src_coords = tf.matmul(t2s_pose, tgt_coords)
    return src_coords


def cam2pixel(cam_coords, intrinsic):
    """
    :param cam_coords: 3D points in source frame (x,y,z,1) [4, height*width]
    :param intrinsic: intrinsic camera matrix [3, 3]
    :return: projected pixel coodrinates (u,v,1) [3, height*width]
    """
    pixel_coords = tf.matmul(intrinsic, cam_coords[:3, :])
    pixel_coords = pixel_coords / (pixel_coords[2, :] + 1e-10)
    return pixel_coords


def reconstruct_image_roundup(pixel_coords, image, depth):
    """
    :param pixel_coords: floating-point pixel coordinates (u,v,1) [3, height*widht]
    :param image: source image [height, width, 3]
    :return: reconstructed image [height, width, 3]
    """
    # pad 1 pixel around image
    top_pad, bottom_pad, left_pad, right_pad = (1, 1, 1, 1)
    paddings = tf.constant([[top_pad, bottom_pad], [left_pad, right_pad], [0, 0]])
    padded_image = tf.pad(image, paddings, "CONSTANT")
    print(padded_image[0:5, 0:5, 0])
    # adjust pixel coordinates for padded image
    pixel_coords = tf.round(pixel_coords[:2, :] + 1)
    # clip pixel coordinates into padded image region as integer
    ih, iw, ic = image.get_shape().as_list()
    u_coords = tf.clip_by_value(pixel_coords[0, :], 0, iw+1)
    v_coords = tf.clip_by_value(pixel_coords[1, :], 0, ih+1)
    # pixel as (v, u) in rows for gather_nd()
    pixel_coords = tf.stack([v_coords, u_coords], axis=1)
    pixel_coords = tf.cast(pixel_coords, tf.int32)

    # sample pixels from padded image
    flat_image = tf.gather_nd(padded_image, pixel_coords)
    # set black in depth-zero region
    depth_vec = tf.reshape(depth, shape=(-1, 1))
    depth_invalid_mask = tf.math.equal(depth_vec, 0)
    zeros = tf.zeros(flat_image.get_shape(), dtype=tf.uint8)
    flat_image = tf.where(depth_invalid_mask, zeros, flat_image)
    recon_image = tf.reshape(flat_image, shape=(ih, iw, ic))

    return recon_image


# --------------------------------------------------------------------------------
# TESTS

def test_pixel_meshgrid():
    height, width = (3, 4)
    x = np.linspace(0, width-1, width)
    y = np.linspace(0, height-1, height)
    XY = pixel_meshgrid(height, width)
    # print("meshgrid result", XY)
    out_height, out_width = XY.get_shape().as_list()
    assert (out_height == 3), f"out_width={out_height}"
    assert (out_width == height*width)
    assert (np.isclose(XY[0, :width].numpy(), x).all())
    assert (np.isclose(XY[0, -width:].numpy(), x).all())
    assert (np.isclose(XY[1, :height].numpy(), y[0]).all())
    assert (np.isclose(XY[1, -height:].numpy(), y[-1]).all())
    print("test_pixel_meshgrid passed")


def test_pixel2cam2pixel():
    height, width = (5, 7)
    intrinsic = tf.constant([[5, 0, width//2], [0, 5, height//2], [0, 0, 1]], dtype=tf.float64)
    depth = tf.ones(shape=(height, width), dtype=tf.float64)*3
    pixel_coords = pixel_meshgrid(height, width)
    cam_coords = pixel2cam(pixel_coords, depth, intrinsic)
    prj_pixel_coords = cam2pixel(cam_coords, intrinsic)
    # print("projected pixel coordinates\n", prj_pixel_coords.numpy())
    assert (np.isclose(pixel_coords.numpy(), prj_pixel_coords.numpy()).all())
    print("test_pixel2cam2pixel passed")


def test_gather_nd():
    mesh = pixel_meshgrid(3, 4)
    mesh = tf.reshape(tf.transpose(mesh[:2, :]), (3, 4, -1))
    # print("mesh", mesh)
    indices = tf.constant([[0, 0], [1, 2]])
    expect = tf.stack((mesh[0, 0, :], mesh[1, 2, :]), axis=0)
    result = tf.gather_nd(mesh, indices)
    # print("gather_nd expect", expect)
    # print("gather_nd result", result)
    assert (np.isclose(expect, result).all())
    print("test_gather_nd passed")


def test_synthesize_view():
    np.set_printoptions(precision=3, suppress=True)
    src_image, tgt_image, tgt_depth, t2s_pose, intrinsic = load_data()

    tgt_recon_image = synthesize_view(src_image, tgt_depth, t2s_pose, intrinsic)

    result = np.concatenate([src_image, tgt_recon_image, tgt_image], axis=1)
    cv2.imshow("synthesized", result)
    cv2.waitKey()
    cv2.imwrite(f"samples/synthesized.jpg", result)


def test():
    np.set_printoptions(precision=3, suppress=True)
    # test_pixel_meshgrid()
    # test_pixel2cam2pixel()
    # test_gather_nd()
    test_synthesize_view()


if __name__ == "__main__":
    test()
