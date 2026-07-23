import android.content.pm.ApplicationInfo;
import android.content.res.AssetManager;
import android.content.res.Configuration;
import android.content.res.Resources;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.os.IBinder;
import android.util.DisplayMetrics;

import java.io.OutputStream;
import java.lang.reflect.Method;

/** Render an installed Android package icon to stdout as PNG. */
public final class IconDump {
    private IconDump() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException("package name is required");
        }
        int size = args.length > 1 ? Integer.parseInt(args[1]) : 192;
        size = Math.max(48, Math.min(size, 512));

        int userId;
        if (args.length > 2) {
            userId = Integer.parseInt(args[2]);
        } else {
            Class<?> activityManagerClass = Class.forName("android.app.ActivityManager");
            Method getCurrentUser = activityManagerClass.getDeclaredMethod("getCurrentUser");
            userId = ((Integer) getCurrentUser.invoke(null)).intValue();
        }

        Class<?> serviceManagerClass = Class.forName("android.os.ServiceManager");
        Method getService = serviceManagerClass.getDeclaredMethod("getService", String.class);
        IBinder packageBinder = (IBinder) getService.invoke(null, "package");
        Class<?> packageManagerStub = Class.forName("android.content.pm.IPackageManager$Stub");
        Method asInterface = packageManagerStub.getDeclaredMethod("asInterface", IBinder.class);
        Object packageManager = asInterface.invoke(null, packageBinder);
        ApplicationInfo applicationInfo = null;
        for (Method method : packageManager.getClass().getMethods()) {
            if (!method.getName().equals("getApplicationInfo") || method.getParameterTypes().length != 3) {
                continue;
            }
            Class<?> flagType = method.getParameterTypes()[1];
            Object flags = flagType == long.class ? Long.valueOf(0L) : Integer.valueOf(0);
            applicationInfo = (ApplicationInfo) method.invoke(packageManager, args[0], flags, userId);
            break;
        }
        if (applicationInfo == null) {
            throw new IllegalArgumentException("package not found: " + args[0]);
        }

        AssetManager assets = AssetManager.class.getDeclaredConstructor().newInstance();
        Method addAssetPath = AssetManager.class.getDeclaredMethod("addAssetPath", String.class);
        addAssetPath.invoke(assets, applicationInfo.sourceDir);
        if (applicationInfo.splitSourceDirs != null) {
            for (String splitSourceDir : applicationInfo.splitSourceDirs) {
                addAssetPath.invoke(assets, splitSourceDir);
            }
        }
        DisplayMetrics metrics = new DisplayMetrics();
        metrics.setTo(Resources.getSystem().getDisplayMetrics());
        Configuration configuration = new Configuration(Resources.getSystem().getConfiguration());
        Resources resources = new Resources(assets, metrics, configuration);
        Drawable icon = resources.getDrawable(applicationInfo.icon, null);

        Bitmap bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        icon.setBounds(0, 0, size, size);
        icon.draw(canvas);
        OutputStream output = System.out;
        if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) {
            throw new IllegalStateException("unable to encode icon");
        }
        output.flush();
    }
}
